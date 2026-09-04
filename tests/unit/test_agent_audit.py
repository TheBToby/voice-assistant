"""Tests for the agent-side audit reporter (agent/audit.py)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

import audit as audit_module
from audit import AuditReporter, build_event


def test_build_event_redacts_transcripts_by_default():
    event = build_event(
        "user_input", room="home", identity="respeaker-1",
        data={"text": "what time is it"}, transcripts_enabled=False, now=1.0,
    )
    assert event is not None
    assert "text" not in event["data"] and event["data"]["redacted"] is True
    kept = build_event(
        "user_input", data={"text": "what time is it"}, transcripts_enabled=True, now=1.0
    )
    assert kept["data"]["text"] == "what time is it"
    assert build_event("unknown-type", now=1.0) is None


def test_reporter_queues_and_respects_enabled_flag():
    reporter = AuditReporter(console_url="http://console:8090", token="t", room="home")
    assert reporter.enabled is True
    reporter.user_input("hello")                      # final -> queued (redacted)
    reporter.agent_reply("hi there")
    reporter.tool_call("set_timer", arguments='{"minutes": 5}', duration_ms=3)
    reporter.event("not-a-type", whatever=1)          # dropped silently
    types = [e["type"] for e in reporter._queue]
    assert types == ["user_input", "agent_reply", "tool.call"]
    assert "text" not in reporter._queue[0]["data"]

    disabled = AuditReporter(console_url="", token="", room="home")
    disabled.user_input("hello")
    assert disabled.enabled is False
    assert len(disabled._queue) == 0


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeAsyncClient:
    posted = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.posted.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(200)


def test_flush_posts_batch_with_bearer_token(monkeypatch):
    import types

    fake_httpx = types.SimpleNamespace(AsyncClient=_FakeAsyncClient)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    _FakeAsyncClient.posted.clear()

    reporter = AuditReporter(console_url="http://console:8090/", token="tok", room="home")
    reporter.event("session.started")
    reporter.event("agent.heartbeat")
    sent = asyncio.run(reporter.flush_once())
    assert sent == 2
    post = _FakeAsyncClient.posted[0]
    assert post["url"] == "http://console:8090/internal/events"
    assert post["headers"]["Authorization"] == "Bearer tok"
    assert len(post["json"]["events"]) == 2
    # queue is empty afterwards; a second flush is a no-op
    assert asyncio.run(reporter.flush_once()) == 0


def test_flush_failure_drops_events_instead_of_raising(monkeypatch):
    class _ExplodingClient(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None):
            raise ConnectionError("console down")

    import types

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=_ExplodingClient))
    reporter = AuditReporter(console_url="http://console:8090", token="tok")
    reporter.event("error", message="x")
    assert asyncio.run(reporter.flush_once()) == 0  # no exception, dropped


def test_session_event_wiring_is_defensive():
    reporter = AuditReporter(console_url="http://c", token="t")

    class _FakeSession:
        def __init__(self):
            self.handlers = {}

        def on(self, name, handler):
            self.handlers[name] = handler

    session = _FakeSession()
    reporter.attach_session(session)
    assert set(session.handlers) == {
        "user_input_transcribed", "conversation_item_added", "function_tools_executed",
    }

    class _Ev:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    # user transcript: only final events are recorded
    session.handlers["user_input_transcribed"](_Ev(text="partial", is_final=False))
    session.handlers["user_input_transcribed"](_Ev(text="final question", is_final=True))

    # assistant reply from conversation item
    session.handlers["conversation_item_added"](
        _Ev(item=_Ev(role="assistant", text_content="Here you go"))
    )
    session.handlers["conversation_item_added"](
        _Ev(item=_Ev(role="user", text_content="ignored"))
    )

    # tool call with output
    session.handlers["function_tools_executed"](
        _Ev(
            function_calls=[_Ev(name="set_timer", arguments='{"minutes": 5}', call_id="c1")],
            function_call_outputs=[_Ev(call_id="c1", output="ok", is_error=False)],
        )
    )

    types = [e["type"] for e in reporter._queue]
    assert types == ["user_input", "agent_reply", "tool.call"]
    assert reporter._queue[0]["data"]["redacted"] is True  # transcripts off
    assert reporter._queue[2]["data"]["tool"] == "set_timer"
