"""Echo-style countdown timers.

Pure-logic service (no livekit imports) so it is fully unit-testable:
see tests/unit/test_timers.py. The assistant wires an async `on_expire`
callback that makes the agent speak the announcement.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

ExpireCallback = Callable[["TimerRecord"], Awaitable[None]]


@dataclass
class TimerRecord:
    id: int
    name: str
    duration_seconds: float
    started_at: float  # loop.time()
    task: asyncio.Task | None = field(default=None, repr=False)

    def remaining_seconds(self, loop: asyncio.AbstractEventLoop) -> float:
        elapsed = loop.time() - self.started_at
        return max(0.0, self.duration_seconds - elapsed)


class TimerService:
    """Manage named countdown timers within one assistant session."""

    def __init__(self, max_timers: int = 8) -> None:
        self._timers: dict[str, TimerRecord] = {}  # key: lowercase name
        self._next_id = 1
        self._max_timers = max_timers

    # ------------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self._timers)

    async def start(
        self,
        seconds: float,
        on_expire: ExpireCallback,
        name: str | None = None,
    ) -> TimerRecord:
        """Start a countdown; calls `on_expire(record)` when it fires."""
        if seconds <= 0:
            raise ValueError("Timer duration must be positive")
        if self.count >= self._max_timers:
            raise RuntimeError(f"Already running {self._max_timers} timers")

        loop = asyncio.get_running_loop()
        name = self._normalize(name) or self._auto_name()
        if name in self._timers:
            raise ValueError(f"A timer named '{name}' is already running")

        record = TimerRecord(
            id=self._next_id,
            name=name,
            duration_seconds=float(seconds),
            started_at=loop.time(),
        )
        self._next_id += 1
        record.task = loop.create_task(self._run(record, on_expire))
        self._timers[name] = record
        logger.info("timer started: %s (%.0fs)", name, seconds)
        return record

    def cancel(self, name_or_id: str | int) -> bool:
        """Cancel a timer by name (case-insensitive) or numeric id."""
        record = self.find(name_or_id)
        if record is None:
            return False
        if record.task is not None:
            record.task.cancel()
        del self._timers[record.name]
        logger.info("timer cancelled: %s", record.name)
        return True

    def find(self, name_or_id: str | int) -> TimerRecord | None:
        key = self._normalize(str(name_or_id))
        if key.isdigit() or isinstance(name_or_id, int):
            target_id = int(key) if key else int(name_or_id)
            for record in self._timers.values():
                if record.id == target_id:
                    return record
        return self._timers.get(key)

    def snapshot(self) -> list[dict]:
        """List of running timers with remaining seconds (for prompts/UI)."""
        loop = asyncio.get_event_loop()
        return [
            {
                "id": r.id,
                "name": r.name,
                "duration_seconds": r.duration_seconds,
                "remaining_seconds": round(r.remaining_seconds(loop), 1),
            }
            for r in self._timers.values()
        ]

    # ------------------------------------------------------------------
    def _normalize(self, name: str | None) -> str:
        return (name or "").strip().lower()

    def _auto_name(self) -> str:
        n = 1
        while f"timer {n}" in self._timers:
            n += 1
        return f"timer {n}"

    async def _run(self, record: TimerRecord, on_expire: ExpireCallback) -> None:
        try:
            await asyncio.sleep(record.duration_seconds)
        except asyncio.CancelledError:
            return
        # fire-and-forget removal, then announce
        self._timers.pop(record.name, None)
        try:
            await on_expire(record)
        except Exception:  # noqa: BLE001 - never crash the session on announce
            logger.exception("timer expiry callback failed for %s", record.name)


async def cancel_all(service: TimerService) -> None:
    for record in list(service.snapshot()):
        service.cancel(record["name"])
    with contextlib.suppress(Exception):
        await asyncio.sleep(0)
