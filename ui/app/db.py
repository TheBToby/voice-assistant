"""SQLite persistence for the web console (stdlib sqlite3 only).

One file in the console data volume holds settings overrides, the device
registry and the audit event log. A single connection guarded by a lock is
plenty for a home deployment; WAL mode keeps reads cheap while events are
appended. No FastAPI imports - unit-testable on the host.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS devices (
    identity      TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'device',
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    last_room     TEXT NOT NULL DEFAULT '',
    session_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    type     TEXT NOT NULL,
    room     TEXT NOT NULL DEFAULT '',
    identity TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


class Database:
    """Small synchronous store; call sites are short-lived request handlers."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # meta (config version, generated secrets, ...)
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def bump_config_version(self) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('config_version', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'config_version'"
            ).fetchone()
        return int(row["value"]) if row else 1

    def config_version(self) -> int:
        raw = self.get_meta("config_version", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # settings overrides
    # ------------------------------------------------------------------
    def get_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_settings(self, updates: dict[str, str], updated_by: str = "") -> None:
        """Upsert overrides; empty string values delete the override."""
        now = time.time()
        with self._lock:
            for key, value in (updates or {}).items():
                if value == "":
                    self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                    continue
                self._conn.execute(
                    "INSERT INTO settings (key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                    (key, value, now, updated_by),
                )
            self._conn.commit()

    def setting_sources(self) -> dict[str, dict]:
        """Per-key override metadata (when/who) for the settings UI."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, updated_at, updated_by FROM settings"
            ).fetchall()
        return {
            row["key"]: {"updated_at": row["updated_at"], "updated_by": row["updated_by"]}
            for row in rows
        }
    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------
    def upsert_device(
        self,
        identity: str,
        *,
        room: str = "",
        kind: str = "",
        seen_ts: float | None = None,
        count_session: bool = False,
    ) -> None:
        now = seen_ts if seen_ts is not None else time.time()
        kind = kind or ("browser" if identity.startswith("web-") else "device")
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (identity, kind, first_seen, last_seen, last_room, "
                "session_count) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET "
                "last_seen = excluded.last_seen, "
                "last_room = CASE WHEN excluded.last_room != '' "
                "                  THEN excluded.last_room ELSE devices.last_room END, "
                "session_count = devices.session_count + excluded.session_count",
                (identity, kind, now, now, room, 1 if count_session else 0),
            )
            self._conn.commit()

    def rename_device(self, identity: str, name: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE devices SET name = ? WHERE identity = ?", (name, identity)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def delete_device(self, identity: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM devices WHERE identity = ?", (identity,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # events (audit log)
    # ------------------------------------------------------------------
    def insert_events(self, events: list[dict]) -> int:
        if not events:
            return 0
        rows = []
        for event in events:
            data = event.get("data", "{}")
            if not isinstance(data, str):
                data = json.dumps(data, default=str)
            rows.append(
                (
                    event["ts"],
                    event["type"],
                    event.get("room", ""),
                    event.get("identity", ""),
                    data,
                )
            )
        with self._lock:
            self._conn.executemany(
                "INSERT INTO events (ts, type, room, identity, data) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def query_events(
        self,
        *,
        event_type: str = "",
        identity: str = "",
        search: str = "",
        limit: int = 200,
        before_id: int | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if event_type:
            clauses.append("type = ?")
            params.append(event_type)
        if identity:
            clauses.append("identity = ?")
            params.append(identity)
        if search:
            clauses.append("data LIKE ?")
            params.append(f"%{search}%")
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "type": row["type"],
                "room": row["room"],
                "identity": row["identity"],
                "data": row["data"],
            }
            for row in rows
        ]

    def clear_events(self, before_ts: float | None = None) -> int:
        with self._lock:
            if before_ts is None:
                cursor = self._conn.execute("DELETE FROM events")
            else:
                cursor = self._conn.execute(
                    "DELETE FROM events WHERE ts < ?", (before_ts,)
                )
            self._conn.commit()
        return cursor.rowcount

    def count_events(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"]) if row else 0

    def last_event_age(self, types: tuple[str, ...], now: float) -> float | None:
        """Age in seconds of the newest event among `types` (None if absent)."""
        if not types:
            return None
        placeholders = ", ".join("?" for _ in types)
        with self._lock:
            row = self._conn.execute(
                f"SELECT MAX(ts) AS newest FROM events WHERE type IN ({placeholders})",
                tuple(types),
            ).fetchone()
        if not row or row["newest"] is None:
            return None
        return max(0.0, now - float(row["newest"]))

