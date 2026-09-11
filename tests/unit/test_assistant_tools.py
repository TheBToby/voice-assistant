"""Tests for the Assistant's built-in timer tools (agent/assistant.py).

livekit-agents is not installed on the host (see conftest.py), so a minimal
fake of livekit.agents is injected into sys.modules before importing the
module - the same stubbing pattern used in test_agent_audit.py for httpx.

Regression: cancel_timer() used to treat the bool returned by
TimerService.cancel() as a TimerRecord, crashing with
"AttributeError: 'bool' object has no attribute 'id'" on every successful
cancellation (and swallowing the timer.cancel event for devices).
"""

import asyncio
import json
import sys
import types
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
sys.path.insert(0, str(AGENT_DIR))


def _install_fake_livekit() -> None:
    if "livekit.agents" in sys.modules:
        return

    livekit = types.ModuleType("livekit")
    agents = types.ModuleType("livekit.agents")

    class Agent:  # matches the kwargs Assistant passes to super().__init__
        def __init__(self, *, instructions="", tools=None, **kwargs):
            self.instructions = instructions
            self.tools = list(tools or [])

    class RunContext:  # never used by the tools under test
        pass

    def function_tool(func=None, **kwargs):  # pass-through decorator
        return func if func is not None else (lambda f: f)

    agents.Agent = Agent
    agents.RunContext = RunContext
    agents.function_tool = function_tool
    agents.mcp = types.SimpleNamespace(MCPToolset=type("MCPToolset", (), {}))
    livekit.agents = agents

    sys.modules.setdefault("livekit", livekit)
    sys.modules["livekit.agents"] = agents


_install_fake_livekit()

from assistant import Assistant  # noqa: E402
from timers import TimerService  # noqa: E402


class _FakeSettings:
    instructions = ""
    language = "en"


class _FakeLocalizer:
    """Records the message keys instead of formatting real i18n strings."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def message(self, key, **kwargs):
        self.calls.append((key, kwargs))
        return f"<{key}>"


def _assistant():
    timers = TimerService()
    localizer = _FakeLocalizer()
    agent = Assistant(
        settings=_FakeSettings(), mcp_toolsets=[], timers=timers,
        localizer=localizer,  # type: ignore[arg-type]
    )
    agent.bind_session(_FakeSession())
    return agent, timers, localizer


class _FakeParticipant:
    def __init__(self, published):
        self._published = published

    async def publish_data(self, data, topic=""):
        self._published.append(json.loads(data.decode()))


class _FakeRoom:
    def __init__(self, published):
        self.local_participant = _FakeParticipant(published)


class _FakeSession:
    """Just enough of AgentSession for Assistant._publish_event()."""

    def __init__(self):
        self.published = []  # decoded JSON payloads sent to the room
        self.room = _FakeRoom(self.published)
        self.said = []  # `say` is only used on timer expiry

    async def say(self, text):
        self.said.append(text)


def _run(coro):
    return asyncio.run(coro)


def test_cancel_timer_publishes_event_and_confirms():
    """Regression: a successful cancellation used to raise
    AttributeError('bool' object has no attribute 'id')."""

    async def scenario():
        agent, timers, localizer = _assistant()
        record = await timers.start(300, lambda r: None, name="pizza")

        reply = await agent.cancel_timer(None, "pizza")

        assert reply == "<timer_cancelled>"
        assert timers.count == 0
        assert localizer.calls == [("timer_cancelled", {"name": "pizza"})]
        assert agent._session.published == [
            {"event": "timer.cancel", "id": record.id, "name": "pizza"}
        ]

    _run(scenario())


def test_cancel_timer_by_number():
    async def scenario():
        agent, timers, _ = _assistant()
        record = await timers.start(300, lambda r: None)  # auto-named

        reply = await agent.cancel_timer(None, str(record.id))

        assert reply == "<timer_cancelled>"
        assert timers.count == 0
        assert agent._session.published == [
            {"event": "timer.cancel", "id": record.id, "name": record.name}
        ]

    _run(scenario())


def test_cancel_unknown_timer_reports_not_found():
    async def scenario():
        agent, timers, localizer = _assistant()
        await timers.start(300, lambda r: None, name="tea")

        reply = await agent.cancel_timer(None, "nope")

        assert reply == "<timer_not_found>"
        assert timers.count == 1  # untouched
        assert agent._session.published == []
        assert localizer.calls[0][0] == "timer_not_found"

    _run(scenario())

