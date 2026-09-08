"""Temporary diagnostics: record incoming (device) audio to WAV files.

Answers "is real voice audio arriving at the agent?" by listening: while
DEBUG_RECORD_AUDIO is enabled (the default while the voice-detection issue
is open), every subscribed remote audio track is written to its own WAV
file under DEBUG_AUDIO_DIR (default: <home>/.cache/debug-audio - the
persistent model-cache volume inside the agent container).

Copy recordings out of the container with:

    docker compose cp agent:/home/agent/.cache/debug-audio ./debug-audio

(or look into $MODEL_CACHE_DIR/debug-audio when a host folder is bound as
the model cache). Recording stops after DEBUG_AUDIO_MAX_SECONDS per track
(default 300) and recordings older than DEBUG_AUDIO_KEEP_DAYS (default 3)
are pruned at session start, so the feature cannot fill the disk
unnoticed. Set DEBUG_RECORD_AUDIO=false once voice detection works.

This module deliberately avoids importing livekit at module level so the
pure helpers stay unit-testable without the heavy runtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from livekit import rtc

logger = logging.getLogger("voice-assistant")

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
DEFAULT_MAX_SECONDS = 300
DEFAULT_KEEP_DAYS = 3.0


def safe_stem(text: str, fallback: str = "unknown") -> str:
    """Filesystem-safe, non-empty file name fragment."""
    cleaned = _UNSAFE_CHARS.sub("-", (text or "").strip()).strip("-.")
    return cleaned[:64].strip("-.") or fallback


def debug_audio_dir(env: dict[str, str] | None = None) -> Path:
    """Directory for recordings (DEBUG_AUDIO_DIR or the model cache)."""
    env = os.environ if env is None else env
    override = (env.get("DEBUG_AUDIO_DIR") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".cache" / "debug-audio"


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def prune_old_files(
    directory: Path, keep_days: float, now: float | None = None
) -> int:
    """Delete *.wav files older than keep_days; returns the removed count."""
    now = time.time() if now is None else now
    removed = 0
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.wav"):
        try:
            if now - path.stat().st_mtime > keep_days * 86400:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info(
            "audio debug: pruned %d recording(s) older than %g day(s)",
            removed,
            keep_days,
        )
    return removed


class RemoteAudioRecorder:
    """Writes every remote audio track of the room to its own WAV file."""

    def __init__(
        self,
        directory: Path | None = None,
        max_seconds: int | None = None,
        keep_days: float | None = None,
    ) -> None:
        self._dir = Path(directory) if directory else debug_audio_dir()
        self._max_seconds = (
            _env_int("DEBUG_AUDIO_MAX_SECONDS", DEFAULT_MAX_SECONDS)
            if max_seconds is None
            else max_seconds
        )
        self._keep_days = (
            _env_float("DEBUG_AUDIO_KEEP_DAYS", DEFAULT_KEEP_DAYS)
            if keep_days is None
            else keep_days
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: dict[Path, wave.Wave_write] = {}
        self._attached = False

    def attach(self, room: rtc.Room) -> None:
        """Start recording; call before room.connect() so the device's
        already-published track is captured too."""
        if self._attached:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        prune_old_files(self._dir, self._keep_days)
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        self._attached = True
        logger.info(
            "audio debug: recording incoming audio to %s (cap: %ds per track)",
            self._dir,
            self._max_seconds,
        )

    async def aclose(self) -> None:
        """Stop all recordings and close the files (shutdown callback)."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for path, writer in list(self._writers.items()):
            self._close_writer(path, writer)

    def _on_track_subscribed(
        self, track: Any, publication: Any, participant: Any
    ) -> None:
        from livekit import rtc

        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        identity = participant.identity or str(participant.sid)
        logger.info(
            "audio debug: audio track subscribed from %r (%s)",
            identity,
            getattr(track, "sid", ""),
        )
        task = asyncio.create_task(self._record(track, participant))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_track_unsubscribed(
        self, track: Any, publication: Any, participant: Any
    ) -> None:
        logger.info(
            "audio debug: audio track unsubscribed from %r",
            participant.identity or str(participant.sid),
        )

    async def _record(self, track: Any, participant: Any) -> None:
        from livekit import rtc

        identity = safe_stem(
            participant.identity or str(participant.sid), "participant"
        )
        track_id = safe_stem(str(getattr(track, "sid", "") or "track"), "track")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self._dir / f"{stamp}-{identity}-{track_id}.wav"
        suffix = 1
        while path.exists():
            suffix += 1
            path = self._dir / f"{stamp}-{identity}-{track_id}-{suffix}.wav"

        audio_stream = rtc.AudioStream(track=track)
        writer: wave.Wave_write | None = None
        frames_written = 0
        sample_rate = 0
        started = time.monotonic()
        last_progress = started
        try:
            async for event in audio_stream:
                frame = event.frame
                if writer is None:
                    writer = wave.open(str(path), "wb")
                    self._writers[path] = writer
                    writer.setnchannels(frame.num_channels)
                    # AudioFrame data is little-endian int16 PCM
                    writer.setsampwidth(2)
                    writer.setframerate(frame.sample_rate)
                    sample_rate = frame.sample_rate
                    logger.info(
                        "audio debug: recording %s (%d Hz, %d channel(s))",
                        path.name,
                        frame.sample_rate,
                        frame.num_channels,
                    )
                writer.writeframes(frame.data)
                frames_written += frame.samples_per_channel
                now = time.monotonic()
                if now - last_progress >= 30:
                    last_progress = now
                    logger.info(
                        "audio debug: %s: %.1f s recorded so far",
                        path.name,
                        frames_written / max(sample_rate, 1),
                    )
                if (now - started) >= self._max_seconds:
                    logger.info(
                        "audio debug: %s hit the %ds cap - closing",
                        path.name,
                        self._max_seconds,
                    )
                    break
        except Exception:
            logger.warning(
                "audio debug: recording %s failed", path.name, exc_info=True
            )
        finally:
            await audio_stream.aclose()
            if writer is not None:
                self._close_writer(path, writer)
            size_kb = path.stat().st_size / 1024 if path.exists() else 0
            seconds = frames_written / max(sample_rate, 1)
            logger.info(
                "audio debug: wrote %s (%.1f s, %.0f kB)",
                path.name,
                seconds,
                size_kb,
            )

    def _close_writer(self, path: Path, writer: wave.Wave_write) -> None:
        self._writers.pop(path, None)
        try:
            writer.close()  # patches the WAV header; safe to call twice
        except Exception:  # noqa: BLE001 - closing must never raise
            logger.debug("audio debug: closing %s failed", path, exc_info=True)