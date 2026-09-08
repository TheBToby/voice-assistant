"""Tests for the temporary audio-recording diagnostics (agent/debug_audio.py).

The recorder itself needs the livekit runtime; the pure helpers are tested
directly.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

import debug_audio


def test_safe_stem():
    assert debug_audio.safe_stem("device-1") == "device-1"
    assert debug_audio.safe_stem("weird name/with:chars") == "weird-name-with-chars"
    assert debug_audio.safe_stem("") == "unknown"
    assert debug_audio.safe_stem("///") == "unknown"
    assert len(debug_audio.safe_stem("x" * 100)) == 64


def test_debug_audio_dir_override(monkeypatch):
    monkeypatch.setenv("DEBUG_AUDIO_DIR", "/tmp/audio-override")
    assert debug_audio.debug_audio_dir() == Path("/tmp/audio-override")
    monkeypatch.delenv("DEBUG_AUDIO_DIR", raising=False)
    assert debug_audio.debug_audio_dir().name == "debug-audio"


def test_prune_old_files(tmp_path):
    old = tmp_path / "old.wav"
    old.write_bytes(b"x")
    fresh = tmp_path / "fresh.wav"
    fresh.write_bytes(b"y")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))

    assert debug_audio.prune_old_files(tmp_path, keep_days=3) == 1
    assert not old.exists()
    assert fresh.exists()
    # non-wav files are never touched, missing directories are fine
    keep = tmp_path / "keep.txt"
    keep.write_bytes(b"z")
    assert debug_audio.prune_old_files(tmp_path, keep_days=3) == 0
    assert keep.exists()
    assert debug_audio.prune_old_files(tmp_path / "missing", keep_days=3) == 0
