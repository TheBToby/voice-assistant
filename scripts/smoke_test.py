#!/usr/bin/env python3
"""End-to-end smoke test for the voice assistant stack.

What it verifies, in order:
  1. The LiveKit HTTP endpoint answers.
  2. A test participant can join a room (token + keys work).
  3. The agent publishes audio back (it speaks the configured greeting),
     i.e. agent dispatch + TTS + WebRTC return path work.

Optional full speech round-trip (STT -> LLM -> TTS):
  --wav tests/assets/hello.wav   publish a speech WAV instead of silence
  --tts-text "hello there"       synthesize the test phrase with ElevenLabs
                                 (needs ELEVEN_API_KEY) and do the same.

Run inside the compose network:
    docker compose --profile smoke run --rm smoke
or locally:  python3 scripts/smoke_test.py --url ws://localhost:7880
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
import wave
from array import array
from datetime import timedelta

import httpx
from livekit import api, rtc

PASS, FAIL = "PASS", "FAIL"


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


async def check_http(url_ws: str, timeout: float) -> bool:
    http_url = url_ws.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(http_url)
        ok = resp.status_code == 200
        log(f"{PASS if ok else FAIL}: LiveKit HTTP endpoint {http_url} -> {resp.text[:40]!r}")
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"{FAIL}: cannot reach LiveKit at {http_url}: {exc}")
        return False


def mint_token(args: argparse.Namespace, identity: str) -> str:
    token = (
        api.AccessToken(args.api_key, args.api_secret)
        .with_identity(identity)
        .with_name("Smoke Test")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=args.room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_ttl(timedelta(minutes=10))
        .to_jwt()
    )
    return token


def load_wav_frames(
    path: str, sample_rate: int, channels: int
) -> list[rtc.AudioFrame]:
    """Read a 16-bit PCM WAV and split it into 20 ms AudioFrames."""
    with contextlib.closing(wave.open(path, "rb")) as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != channels:
            raise ValueError("WAV must be 16-bit PCM mono")
        src_rate = wav.getframerate()
        samples = array("h", wav.readframes(wav.getnframes()))

    if src_rate != sample_rate:  # naive linear resample (smoke test only)
        ratio = sample_rate / src_rate
        out = array("h")
        for i in range(int(len(samples) * ratio)):
            pos = i / ratio
            i0 = int(pos)
            i1 = min(i0 + 1, len(samples) - 1)
            out.append(int(samples[i0] + (samples[i1] - samples[i0]) * (pos - i0)))
        samples = out

    chunk = int(sample_rate * 0.02)
    total = len(samples) // channels
    frames: list[rtc.AudioFrame] = []
    for start in range(0, total, chunk):
        count = min(chunk, total - start)
        frames.append(
            rtc.AudioFrame(
                data=samples[start * channels : (start + count) * channels].tobytes(),
                sample_rate=sample_rate,
                num_channels=channels,
                samples_per_channel=count,
            )
        )
    return frames


async def play_frames(source: rtc.AudioSource, frames: list[rtc.AudioFrame]) -> None:
    """Publish frames in real time (capture_frame paces at 20 ms per frame)."""
    for frame in frames:
        await source.capture_frame(frame)


def rms(frame: rtc.AudioFrame) -> float:
    samples = array("h", bytes(frame.data))
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return (total / len(samples)) ** 0.5 / 32768.0


async def wait_for_agent_audio(
    room: rtc.Room, timeout: float, need_loud: bool
) -> tuple[bool, str]:
    """Wait until audio frames (non-silent if `need_loud`) arrive from the agent."""
    got_frames = 0
    loud_frame_seen = False
    deadline = time.monotonic() + timeout
    readers: list[tuple[rtc.Track, rtc.AudioStream]] = []

    def remote_audio_tracks() -> list[rtc.Track]:
        tracks = []
        for participant in room.remote_participants.values():
            for pub in participant.track_publications.values():
                if pub.track is not None and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    tracks.append(pub.track)
        return tracks

    while time.monotonic() < deadline:
        for track in remote_audio_tracks():
            if not any(t == track for t, _ in readers):
                readers.append((track, rtc.AudioStream(track)))

        for _, stream in readers:
            with contextlib.suppress(TimeoutError):
                event = await asyncio.wait_for(anext(stream), timeout=0.5)
                got_frames += 1
                if rms(event.frame) > 0.005:
                    loud_frame_seen = True

        if got_frames and (loud_frame_seen or not need_loud):
            quality = "non-silent" if loud_frame_seen else "silent"
            return True, f"agent audio flowing ({got_frames} frames, {quality})"
        await asyncio.sleep(0.2)

    if got_frames:
        return (
            False,
            f"agent sent {got_frames} frames but they were all silent - the agent "
            "joined fine but TTS produced no audio (check ELEVEN_API_KEY / "
            "TTS_MODEL / TTS_VOICE_ID, or run with --wav / --tts-text)",
        )
    return False, "agent never published audio (dispatch failed? check agent logs)"


async def run(args: argparse.Namespace) -> int:
    results: list[tuple[str, bool, str]] = []

    # 1. HTTP endpoint
    http_ok = await check_http(args.url, args.timeout)
    results.append(("livekit-http", http_ok, ""))

    # optional: synthesize speech for the round-trip test
    wav_path = args.wav
    if args.tts_text:
        wav_path = await synthesize_with_elevenlabs(args.tts_text, args.eleven_api_key)

    # 2-4. WebRTC round trip
    room = rtc.Room()
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("smoke-mic", source)
    options = rtc.TrackPublishOptions()
    options.source = rtc.TrackSource.SOURCE_MICROPHONE

    identity = f"smoke-{int(time.time())}"
    token = mint_token(args, identity)
    play_task: asyncio.Task | None = None

    try:
        await room.connect(args.url, token)
        log(f"{PASS}: connected to room '{args.room}' as {identity}")
        results.append(("room-join", True, ""))

        await room.local_participant.publish_track(track, options)
        log(f"{PASS}: published microphone track")

        if wav_path:
            frames = load_wav_frames(wav_path, 16000, 1)
            log(f"publishing {len(frames)} audio frames from {wav_path}")
            play_task = asyncio.create_task(play_frames(source, frames))
        else:
            log(
                "no --wav given: streaming silence "
                "(the agent greeting is the audio check)"
            )

        log(f"waiting up to {args.timeout:.0f}s for the agent's audio ...")
        ok, detail = await wait_for_agent_audio(room, args.timeout, need_loud=True)
        results.append(("agent-joins-and-speaks", ok, detail))
        if play_task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(play_task, timeout=10)

    except Exception as exc:  # noqa: BLE001
        log(f"{FAIL}: {exc.__class__.__name__}: {exc}")
        results.append(
            ("webrtc-roundtrip", False, f"{exc.__class__.__name__}: {exc}")
        )
    finally:
        await room.disconnect()

    print("\n===== smoke test summary =====")
    failed = False
    for name, ok, detail in results:
        status = PASS if ok else FAIL
        failed |= not ok
        print(f"  {status}  {name}" + (f"  ({detail})" if detail else ""))
    print("==============================")
    return 1 if failed else 0


async def synthesize_with_elevenlabs(text: str, api_key: str) -> str:
    """Create a temporary speech WAV via ElevenLabs for the full round-trip."""
    import tempfile

    api_key = api_key or os.getenv("ELEVEN_API_KEY", "")
    if not api_key:
        log("SKIP: --tts-text given but ELEVEN_API_KEY missing")
        return ""
    voice_id = os.getenv("TTS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"xi-api-key": api_key},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "output_format": "pcm_16000",
            },
        )
        resp.raise_for_status()
        raw = resp.content

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with contextlib.closing(wave.open(path, "wb")) as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(raw)
    log(f"synthesized test phrase to {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice assistant E2E smoke test")
    parser.add_argument(
        "--url", default=os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    )
    parser.add_argument("--api-key", default=os.getenv("LIVEKIT_API_KEY", "devkey"))
    parser.add_argument("--api-secret", default=os.getenv("LIVEKIT_API_SECRET", ""))
    parser.add_argument("--room", default="smoke-test")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--wav", default="", help="speech WAV (16-bit PCM) to publish")
    parser.add_argument(
        "--tts-text", default="", help="synthesize this phrase via ElevenLabs"
    )
    parser.add_argument("--eleven-api-key", default=os.getenv("ELEVEN_API_KEY", ""))
    args = parser.parse_args()

    if not args.api_secret:
        print("error: LIVEKIT_API_SECRET not set", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())



