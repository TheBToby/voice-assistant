"""The voice assistant agent: ElevenLabs STT/TTS, OpenAI LLM, MCP tools.

Built-in skills (time, timers) are implemented as LiveKit function tools;
Home Assistant, weather and any additional capabilities are provided by
remote MCP servers configured via environment variables. Everything the
agent says on its own behalf follows the language configured via LANGUAGE
(default: German) - see i18n.py.
"""

from __future__ import annotations

import json
import logging

from livekit.agents import Agent, RunContext, function_tool, mcp

import audit as audit_module
from clock import now_text
from config import AgentSettings
from i18n import Localizer
from timers import TimerRecord, TimerService

logger = logging.getLogger(__name__)


class Assistant(Agent):
    def __init__(
        self,
        settings: AgentSettings,
        mcp_toolsets: list[mcp.MCPToolset],
        timers: TimerService | None = None,
        localizer: Localizer | None = None,
        audit: "audit_module.AuditReporter | None" = None,
    ) -> None:
        super().__init__(
            instructions=settings.instructions,
            tools=list(mcp_toolsets),
        )
        self.settings = settings
        self.t = localizer or Localizer(settings.language)
        self.timers = timers or TimerService()
        self.audit = audit
        self._session = None  # bound by the entrypoint once the session exists

    def bind_session(self, session) -> None:  # noqa: ANN001 - AgentSession
        """Give the agent access to the AgentSession for announcements."""
        self._session = session

    # ------------------------------------------------------------------
    # built-in skill: clock
    # ------------------------------------------------------------------
    @function_tool
    async def get_current_time(self, context: RunContext) -> str:
        """Get the current local date and time. Use this whenever the user
        asks for the time, the date, or the day of the week."""

        return now_text(localizer=self.t)

    # ------------------------------------------------------------------
    # built-in skill: timers
    # ------------------------------------------------------------------
    @function_tool
    async def set_timer(
        self,
        context: RunContext,
        minutes: float = 0,
        seconds: float = 0,
        name: str = "",
    ) -> str:
        """Start a countdown timer that announces itself when it expires.

        Args:
            minutes: duration in minutes (can be fractional, e.g. 0.5)
            seconds: additional seconds; minutes + seconds is the total
            name: optional short label, e.g. "pizza" or "laundry"
        """
        total = float(minutes) * 60 + float(seconds)
        if total <= 0:
            return self.t.message("timer_duration_invalid")

        try:
            record = await self.timers.start(
                total, self._announce_timer_expired, name=name or None
            )
        except (ValueError, RuntimeError) as exc:
            return self.t.message("timer_start_failed", reason=exc)

        logger.info("tool:set_timer -> %s (%.0fs)", record.name, total)
        duration = self.t.format_duration(total)
        if record.name.startswith("timer "):
            return self.t.message("timer_started", duration=duration)
        return self.t.message(
            "timer_started_named", name=record.name, duration=duration
        )

    @function_tool
    async def cancel_timer(self, context: RunContext, name: str) -> str:
        """Cancel a running timer by its name (or number).

        Args:
            name: the timer's name, e.g. "pizza", or its number, e.g. "2"
        """
        record = self.timers.cancel(name)
        if record:
            return self.t.message("timer_cancelled", name=name)
        running = (
            ", ".join(t["name"] for t in self.timers.snapshot())
            or self.t.message("none")
        )
        return self.t.message("timer_not_found", name=name, running=running)

    @function_tool
    async def list_timers(self, context: RunContext) -> str:
        """List the currently running timers with their remaining time."""
        snapshot = self.timers.snapshot()
        if not snapshot:
            return self.t.message("no_timers_running")
        lines = [
            self.t.message(
                "timer_remaining",
                name=t["name"],
                duration=self.t.format_duration(t["remaining_seconds"]),
            )
            for t in snapshot
        ]
        return self.t.message("running_timers", list="\n".join(lines))

    # ------------------------------------------------------------------
    async def _announce_timer_expired(self, record: TimerRecord) -> None:
        """Speak the expiry announcement and notify room participants."""
        text = self.t.message("timer_expired", name=record.name.capitalize())
        if self.audit is not None:
            self.audit.event("timer.expired", name=record.name)
        if self._session is not None:
            await self._session.say(text)
        await self._publish_event("timer.expired", {"name": record.name})

    async def _publish_event(self, event: str, payload: dict) -> None:
        """Best-effort data message so devices can react (LEDs, displays)."""
        if self._session is None:
            return
        try:
            message = json.dumps({"event": event, **payload})
            room = self._session.room
            await room.local_participant.publish_data(
                message.encode(), topic="assistant.event"
            )
        except Exception:  # noqa: BLE001
            logger.debug("could not publish event %s", event, exc_info=True)
