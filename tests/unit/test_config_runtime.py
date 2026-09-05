"""Tests for console-driven runtime configuration (agent/config.py)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

import config
from config import AgentSettings, _console_url, _internal_token, apply_overrides


def base_env() -> dict:
    return {
        "LIVEKIT_URL": "ws://localhost:7880",
        "LIVEKIT_API_KEY": "devkey",
        "LIVEKIT_API_SECRET": "secret",
        "ELEVEN_API_KEY": "eleven",
        "OPENAI_API_KEY": "openai",
    }


def test_console_defaults_from_env():
    settings = AgentSettings.from_env(base_env())
    assert settings.console_url == "http://localhost:8090"
    assert settings.console_token == _internal_token(base_env())
    assert settings.console_token  # derived from LIVEKIT_API_SECRET
    assert settings.audit_enabled is True
    assert settings.transcripts_enabled is False
    assert settings.extra_mcp_specs == ()

    assert _console_url({"CONSOLE_URL": "http://console:9999/"}) == "http://console:9999"
    assert _console_url({"UI_PORT": "9000"}) == "http://localhost:9000"
    assert _console_url(base_env()) == "http://localhost:8090"
    # explicit token wins over the derived one
    env = base_env() | {"CONSOLE_INTERNAL_TOKEN": "explicit"}
    assert AgentSettings.from_env(env).console_token == "explicit"
    # audit can be disabled via env
    env = base_env() | {"CONSOLE_AUDIT_DISABLED": "true"}
    assert AgentSettings.from_env(env).audit_enabled is False


def test_console_token_matches_console_derivation():
    """Agent and console must derive the same internal token."""
    import hashlib
    import hmac

    secret = "shared-secret"
    expected = hmac.new(
        secret.encode(), b"voice-assistant/console-internal", hashlib.sha256
    ).hexdigest()[:40]
    assert _internal_token({"LIVEKIT_API_SECRET": secret}) == expected


def test_apply_overrides_maps_settings_and_transcripts():
    base = AgentSettings.from_env(base_env())
    payload = {
        "version": 3,
        "settings": {
            "assistant_name": "Jarvis",
            "language": "en",
            "greeting": "Hello.",
            "enable_turn_detector": "false",
            "llm_model": "llama3.1:8b",
            "home_assistant_url": "http://ha:8123",
            "home_assistant_token": "ha-token",
        },
        "transcripts_enabled": True,
    }
    settings = apply_overrides(base, payload)
    assert settings.assistant_name == "Jarvis"
    assert settings.language == "en"
    assert settings.greeting == "Hello."
    assert settings.enable_turn_detector is False
    assert settings.llm_model == "llama3.1:8b"
    assert settings.transcripts_enabled is True
    assert settings.llm_model != base.llm_model  # a new object was built
    # unmapped keys are ignored, base values survive
    assert settings.stt_model == base.stt_model


def test_apply_overrides_mcp_servers_shadow_env_and_ha():
    env = base_env() | {
        "HOME_ASSISTANT_URL": "http://ha:8123",
        "HOME_ASSISTANT_TOKEN": "ha-token",
        "MCP_SERVERS_JSON": json.dumps(
            [{"id": "weather", "url": "http://env-weather/mcp"}]
        ),
    }
    base = AgentSettings.from_env(env)
    payload = {
        "settings": {},
        "mcp_servers": [
            {"id": "weather", "url": "http://ui-weather/mcp",
             "headers": {"X-API-Key": "abc"}},
            {"id": "home-assistant", "url": "http://other-ha/mcp", "headers": {}},
        ],
    }
    servers: dict = {}
    for spec in apply_overrides(base, payload).mcp_servers():
        servers.setdefault(spec.id, spec)  # first definition wins
    # console-managed entries shadow env/HA definitions with the same id
    assert servers["weather"].url == "http://ui-weather/mcp"
    assert servers["weather"].headers == {"X-API-Key": "abc"}
    assert servers["home-assistant"].url == "http://other-ha/mcp"
    # malformed entries are skipped, not fatal
    payload_bad = {"settings": {}, "mcp_servers": [{"url": "missing-id"}, "junk"]}
    assert apply_overrides(base, payload_bad) is not None


def test_apply_overrides_tolerates_garbage():
    base = AgentSettings.from_env(base_env())
    assert apply_overrides(base, "not-a-dict") is base
    assert apply_overrides(base, {}) is base
    assert apply_overrides(base, {"settings": "junk"}) is base


def test_apply_overrides_maps_assistant_instructions_to_prompt():
    """Regression: the console key is 'assistant_instructions' but the
    AgentSettings field is 'instructions' - the override must land on the
    field instead of raising TypeError (which crash-looped every job)."""
    base = AgentSettings.from_env(base_env())
    payload = {"settings": {"assistant_instructions": "You are a pirate."}}
    settings = apply_overrides(base, payload)
    assert settings.instructions == "You are a pirate."
    # empty console value = "use the built-in default prompt" (from_env rule)
    settings = apply_overrides(base, {"settings": {"assistant_instructions": ""}})
    assert settings.instructions == base.instructions
    assert "Atlas" in settings.instructions


def test_apply_overrides_tolerates_console_key_drift(monkeypatch):
    """An allowlisted console key without a matching AgentSettings field
    (ui/ and agent/ deployments drifting apart) is dropped, not fatal."""
    monkeypatch.setattr(
        config,
        "_CONSOLE_SETTING_FIELDS",
        config._CONSOLE_SETTING_FIELDS + ("some_future_setting",),
    )
    base = AgentSettings.from_env(base_env())
    payload = {
        "settings": {"assistant_name": "Jarvis", "some_future_setting": "x"},
    }
    settings = apply_overrides(base, payload)
    assert settings.assistant_name == "Jarvis"
    assert settings.greeting == base.greeting
