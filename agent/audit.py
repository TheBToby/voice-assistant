"""Best-effort audit reporting from the agent to the web console.

Like config.py this module is deliberately free of livekit imports so its
logic is unit-testable on the host (tests/unit/test_agent_audit.py); the
AgentSession event wiring is done defensively via attach_session().

Design goals:
* the voice pipeline must never break because of diagnostics - every post
  is queued, flushed on a background task and dropped on failure;
* speech content (transcripts) is only included when the console setting
  "Store transcripts" is enabled (default: off). The console enforces the
  same rule on ingestion, so the two sides agree even if settings change
  mid-session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger("voice-assistant.audit")

# mirror of the console's accepted types (ui/app/audit_core.py)
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session.started", "session.ended", "device.join", "device.leave",
        "user_input", "agent_reply", "tool.call", "timer.expired",
        "agent.ready", "agent.heartbeat", "error",
    }
)
TEXT_EVENT_TYPES: frozenset[str] = frozenset({"user_input", "agent_reply"})

MAX_TEXT_LEN = 4000
MAX_IDENTITY_LEN = 256
MAX_QUEUE = 500
FLUSH_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 30.0
POST_TIMEOUT = 3.0


def clip_text(value: object, limit: int = MAX_TEXT_LEN) -> str:
    return "" if value is None else str(value)[:limit]


def build_event(
    event_type: str,
    *,
    room: str = "",
    identity: str = "",
    data: dict | None = None,
    transcripts_enabled: bool = False,
    now: float | None = None,
) -> dict | None:
    """Normalized audit event; None when the type is unknown (never posted)."""
    if event_type not in EVENT_TYPES:
        return None
    payload = dict(data or {})
    if event_type in TEXT_EVENT_TYPES and not transcripts_enabled:
        payload = {k: v for k, v in payload.items() if k != "text"}
        payload["redacted"] = True
    return {
        "ts": now if now is not None else time.time(),
        "type": event_type,
        "room": clip_text(room, MAX_IDENTITY_LEN),
        "identity": clip_text(identity, MAX_IDENTITY_LEN),
        "data": payload,
    }


class AuditReporter:
    """Queues events in the event loop and posts batches to the console."""

    def __init__(self, *, console_url: str, token: str, room: str = "") -> None:
        self._console_url = (console_url or "").rstrip("/")
        self._token = token
        self._room = room
        self._agent_identity = ""
        self._transcripts = False
        self._queue: deque[dict] = deque(maxlen=MAX_QUEUE)
        self._task: asyncio.Task | None = None
        self._last_heartbeat = 0.0
        self._stopping = False

    # ------------------------------------------------------------------
    def configure(
        self, *, room: str = "", agent_identity: str = "", transcripts: bool = False
    ) -> None:
        self._room = room or self._room
        self._agent_identity = agent_identity or self._agent_identity
        self._transcripts = bool(transcripts)

    @property
    def enabled(self) -> bool:
        return bool(self._console_url and self._token)

    def event(self, event_type: str, identity: str = "", **data) -> None:
        """Queue one event (synchronous, never raises)."""
        if not self.enabled or self._stopping:
            return
        event = build_event(
            event_type,
            room=self._room,
            identity=identity,
            data=data,
            transcripts_enabled=self._transcripts,
        )
        if event is not None:
            self._queue.append(event)

    def user_input(self, text: str, *, final: bool = True) -> None:
        if final:
            self.event("user_input", text=clip_text(text))

    def agent_reply(self, text: str) -> None:
        self.event("agent_reply", text=clip_text(text))

    def tool_call(
        self, name: str, arguments: str = "", duration_ms: int = 0,
        error: str = "", identity: str = "",
    ) -> None:
        self.event(
            "tool.call",
            identity=identity,
            tool=clip_text(name, 128),
            arguments=clip_text(arguments, 1000),
            duration_ms=duration_ms,
            error=clip_text(error, 500),
        )
    # ------------------------------------------------------------------
    # background flush loop
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None and self.enabled:
            self._task = asyncio.create_task(self.run())

    async def run(self) -> None:
        """Flush every FLUSH_INTERVAL and heartbeat every HEARTBEAT_INTERVAL."""
        try:
            while not self._stopping:
                await asyncio.sleep(FLUSH_INTERVAL)
                await self.flush_once()
                if time.time() - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                    self.event("agent.heartbeat")
                    self._last_heartbeat = time.time()
                    await self.flush_once()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - diagnostics must never crash us
            logger.debug("audit loop stopped", exc_info=True)

    async def flush_once(self) -> int:
        """POST the queued events; returns how many were sent."""
        if not self._queue:
            return 0
        batch: list[dict] = []
        while self._queue:
            batch.append(self._queue.popleft())
        try:
            import httpx

            async with httpx.AsyncClient(timeout=POST_TIMEOUT) as client:
                response = await client.post(
                    f"{self._console_url}/internal/events",
                    json={"events": batch},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                if response.status_code >= 400:
                    logger.debug(
                        "audit post failed: HTTP %s", response.status_code
                    )
                    return 0
            return len(batch)
        except Exception:  # noqa: BLE001 - drop, never block the pipeline
            logger.debug("audit post failed (console unreachable?)", exc_info=True)
            return 0

    async def aclose(self) -> None:
        """Stop the loop and make a final best-effort flush."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.flush_once()

    # ------------------------------------------------------------------
    # AgentSession wiring (all defensive: event set differs between the
    # pinned livekit-agents 1.5.x and newer releases)
    # ------------------------------------------------------------------
    def attach_session(self, session) -> None:  # noqa: ANN001 - AgentSession
        self._wire(session, "user_input_transcribed", self._on_user_input)
        self._wire(session, "conversation_item_added", self._on_item_added)
        self._wire(session, "function_tools_executed", self._on_tools_executed)

    @staticmethod
    def _wire(session, event_name: str, handler) -> None:
        try:
            session.on(event_name, handler)
        except Exception:  # noqa: BLE001 - unknown event on other versions
            logger.debug("could not attach audit handler for %s", event_name)

    def _on_user_input(self, event) -> None:  # noqa: ANN001
        try:
            if getattr(event, "is_final", False):
                self.user_input(getattr(event, "text", ""))
        except Exception:  # noqa: BLE001
            logger.debug("audit: user_input handler failed", exc_info=True)

    def _on_item_added(self, event) -> None:  # noqa: ANN001
        try:
            item = getattr(event, "item", None)
            role = str(getattr(item, "role", ""))
            if role in ("assistant", "agent"):
                self.agent_reply(getattr(item, "text_content", "") or "")
        except Exception:  # noqa: BLE001
            logger.debug("audit: item handler failed", exc_info=True)

    def _on_tools_executed(self, event) -> None:  # noqa: ANN001
        try:
            calls = getattr(event, "function_calls", None) or []
            outputs = getattr(event, "function_call_outputs", None) or []
            outputs_by_id = {
                getattr(out, "call_id", ""): out for out in outputs
            }
            for call in calls:
                output = outputs_by_id.get(getattr(call, "call_id", ""), None)
                self.tool_call(
                    name=getattr(call, "name", ""),
                    arguments=getattr(call, "arguments", ""),
                    error=(
                        clip_text(getattr(output, "output", ""), 500)
                        if output is not None and getattr(output, "is_error", False)
                        else ""
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.debug("audit: tools handler failed", exc_info=True)

