"""Audit event normalization and retention math for the web console.

Pure logic (no FastAPI imports) so it can be unit-tested on the host.

The agent posts interaction events (session lifecycle, user input, tool
calls, ...) to the console; the console itself records configuration changes
and logins. Everything passes through normalize_event() so the stored shape
is uniform and - importantly - speech content is dropped unless the user
explicitly enabled transcript storage.
"""

from __future__ import annotations

import csv
import io
import json

# every event type the console accepts (agent + console originated)
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session.started",   # agent: room session began
        "session.ended",     # agent: room session finished
        "device.join",       # agent: participant connected
        "device.leave",      # agent: participant disconnected
        "user_input",        # agent: final user utterance (text only if enabled)
        "agent_reply",       # agent: assistant utterance (text only if enabled)
        "tool.call",         # agent: function/MCP tool executed
        "timer.expired",     # agent: countdown timer fired
        "agent.ready",       # agent: worker entrypoint ran
        "agent.heartbeat",   # agent: liveness ping
        "error",             # agent: pipeline error
        "config.changed",    # console: settings/MCP servers updated
        "token.minted",      # console: access token minted from the UI
        "auth.login",        # console: successful login
        "auth.failed",       # console: failed login attempt
    }
)

# event types that may carry speech content (subject to the transcript flag)
TEXT_EVENT_TYPES: frozenset[str] = frozenset({"user_input", "agent_reply"})

MAX_TEXT_LEN = 4000
MAX_IDENTITY_LEN = 256
MAX_JSON_LEN = 8000
DEFAULT_RETENTION_DAYS = 30
SECONDS_PER_DAY = 86400


def clip_text(value: object, limit: int = MAX_TEXT_LEN) -> str:
    """Coerce to a bounded string (speech content and errors can be long)."""
    text = "" if value is None else str(value)
    return text[:limit]


def retention_cutoff(days: int, now: float) -> float:
    """Events older than this timestamp are deleted by the retention task."""
    return now - max(1, int(days)) * SECONDS_PER_DAY


def normalize_event(
    raw: object, *, transcripts_enabled: bool, now: float
) -> dict | None:
    """Validate/normalize one incoming event; None when it must be rejected.

    - unknown event types are rejected (keeps the log queryable)
    - speech content (text) is stripped from user_input/agent_reply events
      unless transcript storage is explicitly enabled
    - all fields are length-bounded so a runaway agent cannot bloat the DB
    - `data` stays a dict here; the store serializes it
    """
    if not isinstance(raw, dict):
        return None
    event_type = str(raw.get("type", "")).strip()
    if event_type not in EVENT_TYPES:
        return None

    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}

    if event_type in TEXT_EVENT_TYPES and not transcripts_enabled:
        data = {k: v for k, v in data.items() if k != "text"}
        data["redacted"] = True
    data = {
        k: clip_text(v) if isinstance(v, str) else v for k, v in data.items()
    }

    try:
        import math

        ts = float(raw.get("ts") or now)
        if not math.isfinite(ts) or ts < 0:
            ts = now
    except (TypeError, ValueError):
        ts = now

    identity = clip_text(raw.get("identity") or "", MAX_IDENTITY_LEN)
    if identity.lower().startswith("agent-"):
        # the assistant's own participant identity is not a "device"
        identity = ""
        data = {k: v for k, v in data.items() if k != "identity"}

    return {
        "ts": ts,
        "type": event_type,
        "room": clip_text(raw.get("room") or "", MAX_IDENTITY_LEN),
        "identity": identity,
        "data": data,
    }


def parse_stored_event(row: dict) -> dict:
    """Deserialize one DB row into an API/UI friendly dict."""
    try:
        data = json.loads(row.get("data") or "{}")
    except ValueError:
        data = {"raw": row.get("data")}
    return {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "type": row.get("type"),
        "room": row.get("room", ""),
        "identity": row.get("identity", ""),
        "data": data,
    }


CSV_FIELDS = ("ts", "type", "room", "identity", "data")


def events_to_csv(events: list[dict]) -> str:
    """Render events as CSV (data column stays JSON)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for event in events:
        writer.writerow(
            {
                "ts": event.get("ts"),
                "type": event.get("type"),
                "room": event.get("room", ""),
                "identity": event.get("identity", ""),
                "data": json.dumps(event.get("data", {}), default=str),
            }
        )
    return buffer.getvalue()
