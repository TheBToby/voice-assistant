"""Persistence guarantees of the console SQLite store (ui/app/db.py).

Devices must survive console restarts - the file is the source of truth and
is kept in the console data volume (docker-compose mounts it at /data).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui" / "app"))

from db import Database

NOW = 1_700_000_000.0


def test_devices_persist_across_reopen(tmp_path):
    db_path = str(tmp_path / "console.db")

    db = Database(db_path)
    db.upsert_device("respeaker-1", room="home", seen_ts=NOW, count_session=True)
    db.rename_device("respeaker-1", "Kitchen speaker")
    db.set_settings({"language": "en"}, updated_by="a@b.c")
    db.insert_events(
        [{"ts": NOW, "type": "session.started", "room": "home",
          "identity": "", "data": "{}"}]
    )
    db.close()

    reopened = Database(db_path)
    devices = {d["identity"]: d for d in reopened.list_devices()}
    assert devices["respeaker-1"]["name"] == "Kitchen speaker"
    assert devices["respeaker-1"]["last_room"] == "home"
    assert devices["respeaker-1"]["session_count"] == 1
    assert reopened.get_settings()["language"] == "en"
    assert reopened.count_events() == 1
    reopened.close()


def test_event_retention_never_touches_devices():
    db = Database(":memory:")
    db.upsert_device("respeaker-1", room="home", seen_ts=NOW)
    db.insert_events(
        [
            {"ts": NOW - 10_000, "type": "error", "room": "", "identity": "",
             "data": "{}"},
            {"ts": NOW, "type": "device.join", "room": "home",
             "identity": "respeaker-1", "data": "{}"},
        ]
    )
    removed = db.clear_events(before_ts=NOW - 5_000)
    assert removed == 1
    # the device row survives the retention cleanup
    assert [d["identity"] for d in db.list_devices()] == ["respeaker-1"]
    db.close()


def test_minted_identity_registers_device_once():
    """The console registers devices when tokens are minted (upsert only)."""
    db = Database(":memory:")
    db.upsert_device("respeaker-1", room="home", seen_ts=NOW)
    db.upsert_device("respeaker-1", room="home", seen_ts=NOW + 5)
    devices = db.list_devices()
    assert len(devices) == 1
    assert devices[0]["session_count"] == 0  # minting is not a session
    assert devices[0]["kind"] == "device"
    db.upsert_device("web-ab12", room="home", seen_ts=NOW + 6)
    kinds = {d["identity"]: d["kind"] for d in db.list_devices()}
    assert kinds["web-ab12"] == "browser"
    db.close()