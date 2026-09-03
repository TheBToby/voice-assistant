"""Unit tests for pure helpers in scripts/smoke_test.py.

The smoke test imports the livekit SDK and httpx, which are not installed on
the host - so we stub both modules BEFORE importing the script. This lets us
test the pure logic (WAV parsing, chunking, RMS math) without heavy deps.
"""

import contextlib
import sys
import tempfile
import types
import wave
from array import array
from pathlib import Path


class StubAudioFrame:
    def __init__(
        self, data: bytes, sample_rate: int, num_channels: int,
        samples_per_channel: int,
    ) -> None:
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


def _make_stub_modules() -> None:
    livekit = types.ModuleType("livekit")
    api_mod = types.ModuleType("livekit.api")
    rtc_mod = types.ModuleType("livekit.rtc")
    rtc_mod.AudioFrame = StubAudioFrame
    livekit.api = api_mod
    livekit.rtc = rtc_mod
    sys.modules.setdefault("livekit", livekit)
    sys.modules.setdefault("livekit.api", api_mod)
    sys.modules.setdefault("livekit.rtc", rtc_mod)

    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object  # placeholder, not used by the tested helpers
    sys.modules.setdefault("httpx", httpx)


_make_stub_modules()

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from smoke_test import load_wav_frames, rms  # noqa: E402


def _write_wav(path: str, samples: array, rate: int = 16000) -> None:
    with contextlib.closing(wave.open(path, "wb")) as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


def test_load_wav_frames_splits_into_20ms_chunks():
    rate = 16000
    samples = array("h", [16000 if i % 2 else -16000 for i in range(rate)])
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        _write_wav(tmp.name, samples, rate)
        frames = load_wav_frames(tmp.name, rate, 1)

    # 1 second @ 20 ms chunks = 50 frames of 320 samples each
    assert len(frames) == 50
    assert all(f.samples_per_channel == 320 for f in frames)
    assert all(f.sample_rate == rate for f in frames)
    assert all(f.num_channels == 1 for f in frames)


def test_load_wav_frames_resamples_other_rates():
    rate_in = 8000
    samples = array("h", [1000, -1000] * (rate_in // 2))  # 1 s square wave @ 8 kHz
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        _write_wav(tmp.name, samples, rate_in)
        frames = load_wav_frames(tmp.name, 16000, 1)

    total_samples = sum(f.samples_per_channel for f in frames)
    assert 15000 <= total_samples <= 17000  # ~2x upsample to 16 kHz


def test_rms_detects_loud_vs_silent_frames():
    rate = 16000
    loud = array("h", [16000, -16000] * 160)  # 320 samples, full amplitude
    silent = array("h", [0] * 320)
    assert rms(StubAudioFrame(loud.tobytes(), rate, 1, 320)) > 0.1
    assert rms(StubAudioFrame(silent.tobytes(), rate, 1, 320)) == 0.0
