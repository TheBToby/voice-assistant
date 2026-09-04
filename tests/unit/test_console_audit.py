"""Tests for audit event normalization, retention and the console DB store."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui" / "app"))

import audit_core as ac
from db import Database

NOW = 1_700_000_000.0


def test_normalize_strips_text_when_transcripts_disabled():
    event = ac.normalize_event(
        {"type": "user_input", "room": "home", "identity": "respeaker-1",
         "data": {"text": "secret utterance"}},
        transcripts_enabled=False,
        now=NOW,
    )
    assert event is not None
    assert "text" not in event["data"]
    assert event["data"]["redacted"] is True
    assert event["ts"] == NOW


def test_normalize_keeps_text_when_enabled():
    event = ac.normalize_event(
        {"type": "agent_reply", "data": {"text": "Hallo!"}},
        transcripts_enabled=True,
        now=NOW,
    )
    assert event["data"]["text"] == "Hallo!"
    assert "redacted" not in event["data"]


def test_normalize_rejects_unknown_and_malformed():
    assert ac.normalize_event({"type": "hax"}, transcripts_enabled=False, now=NOW) is None
    assert ac.normalize_event("not-a-dict", transcripts_enabled=False, now=NOW) is None
    assert ac.normalize_event({}, transcripts_enabled=False, now=NOW) is None


def test_normalize_clips_and_filters_agent_identity():
    event = ac.normalize_event(
        {"type": "device.join", "identity": "agent-12345678",
         "data": {"text": "x" * 99999, "name": "y" * 9999}},
        transcripts_enabled=True,
        now=NOW,
    )
    assert event["identity"] == ""  # the agent itself is not a device
    assert len(event["data"]["text"]) == ac.MAX_TEXT_LEN
    assert event["room"] == ""
    bad_ts = ac.normalize_event({"type": "error", "ts": "nan"}, transcripts_enabled=False, now=NOW)
    assert bad_ts["ts"] == NOW


def test_retention_cutoff_and_csv():
    assert ac.retention_cutoff(30, NOW) == NOW - 30 * 86400
    csv_text = ac.events_to_csv(
        [{"ts": NOW, "type": "auth.login", "room": "", "identity": "",
          "data": {"user": "a@b.c"}}]
    )
    assert "ts,type,room,identity,data" in csv_text
    assert "auth.login" in csv_text


def test_database_roundtrip():
    db = Database(":memory:")
    # settings
    db.set_settings({"language": "en", "greeting": ""}, updated_by="a@b.c")
    db.set_settings({"greeting": "Hi"}, updated_by="a@b.c")
    assert db.get_settings() == {"language": "en", "greeting": "Hi"}
    # empty value deletes the override
    db.set_settings({"greeting": ""})
    assert db.get_settings() == {"language": "en"}
    assert db.setting_sources()["language"]["updated_by"] == "a@b.c"

    # config version counter
    assert db.config_version() == 0
    assert db.bump_config_version() == 1
    assert db.bump_config_version() == 2

    # devices
    db.upsert_device("respeaker-1", room="home", seen_ts=NOW)
    db.upsert_device("respeaker-1", room="kitchen", seen_ts=NOW + 10, count_session=True)
    devices = {d["identity"]: d for d in db.list_devices()}
    assert devices["respeaker-1"]["last_room"] == "kitchen"
    assert devices["respeaker-1"]["session_count"] == 1
    assert devices["respeaker-1"]["kind"] == "device"
    db.upsert_device("web-ab12", seen_ts=NOW)
    assert db.list_devices()[0]["identity"] in ("web-ab12", "respeaker-1")
    assert any(d["kind"] == "browser" for d in db.list_devices())
    assert db.rename_device("respeaker-1", "Kitchen speaker") is True
    assert db.delete_device("web-ab12") is True

    # events
    db.insert_events(
        [
            {"ts": NOW, "type": "session.started", "room": "home", "identity": "",
             "data": "{}"},
            {"ts": NOW + 1, "type": "auth.login", "room": "", "identity": "",
             "data": '{"user": "a@b.c"}'},
        ]
    )
    assert db.count_events() == 2
    assert [e["type"] for e in db.query_events(event_type="auth.login")] == ["auth.login"]
    assert [e["type"] for e in db.query_events(search="a@b.c")] == ["auth.login"]
    page = db.query_events(limit=1)
    assert len(page) == 1 and page[0]["type"] == "auth.login"  # newest first
    assert [e["type"] for e in db.query_events(before_id=page[0]["id"])] == [
        "session.started"
    ]
    # retention: events at NOW/NOW+1 are NEWER than the 30-day cutoff -> kept
    assert db.clear_events(before_ts=ac.retention_cutoff(30, NOW + 1)) == 0
    assert db.count_events() == 2
    # events older than the cutoff are removed
    db.insert_events(
        [{"ts": ac.retention_cutoff(30, NOW) - 1, "type": "error", "room": "",
          "identity": "", "data": "{}"}]
    )
    assert db.clear_events(before_ts=ac.retention_cutoff(30, NOW)) == 1
    assert db.count_events() == 2

    # heartbeat recency
    db.insert_events(
        [{"ts": NOW, "type": "agent.heartbeat", "room": "", "identity": "", "data": "{}"}]
    )
    assert db.last_event_age(("agent.heartbeat",), now=NOW + 5) == 5.0
    # a type that was never recorded has no age
    assert db.last_event_age(("token.minted",), now=NOW + 5) is None
    db.close()
