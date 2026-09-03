"""The voice assistant agent: ElevenLabs STT/TTS, OpenAI LLM, MCP tools.

Built-in skills (time, timers) are implemented as LiveKit function tools;
Home Assistant, weather and any additional capabilities are provided by
remote MCP servers configured via environment variables.
"""

from __future__ import annotations

import json
import logging

from livekit.agents import Agent, RunContext, function_tool, mcp

from clock import format_duration, now_text
from config import AgentSettings
from timers import TimerRecord, TimerService

logger = logging.getLogger(__name__)


class Assistant(Agent):
    def __init__(
        self,
        settings: AgentSettings,
        mcp_toolsets: list[mcp.MCPToolset],
        timers: TimerService | None = None,
    ) -> None:
        super().__init__(
            instructions=settings.instructions,
            tools=list(mcp_toolsets),
        )
        self.settings = settings
        self.timers = timers or TimerService()
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

        return now_text()

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
            return "The timer duration must be positive."

        try:
            record = await self.timers.start(
                total, self._announce_timer_expired, name=name or None
            )
        except (ValueError, RuntimeError) as exc:
            return f"Could not start the timer: {exc}"

        label = f" called '{record.name}'" if not record.name.startswith("timer ") else ""
        logger.info("tool:set_timer -> %s (%.0fs)", record.name, total)
        return (
            f"Timer{label} started for {format_duration(total)}."
        )

    @function_tool
    async def cancel_timer(self, context: RunContext, name: str) -> str:
        """Cancel a running timer by its name (or number).

        Args:
            name: the timer's name, e.g. "pizza", or its number, e.g. "2"
        """
        record = self.timers.cancel(name)
        if record:
            return f"Timer '{name}' cancelled."
        running = ", ".join(t["name"] for t in self.timers.snapshot()) or "none"
        return f"No timer named '{name}' is running. Running timers: {running}."

    @function_tool
    async def list_timers(self, context: RunContext) -> str:
        """List the currently running timers with their remaining time."""
        snapshot = self.timers.snapshot()
        if not snapshot:
            return "No timers are running."
        lines = [
            f"- {t['name']}: {format_duration(t['remaining_seconds'])} remaining"
            for t in snapshot
        ]
        return "Running timers:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    async def _announce_timer_expired(self, record: TimerRecord) -> None:
        """Speak the expiry announcement and notify room participants."""
        text = f"{record.name.capitalize()} timer is done!"
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
