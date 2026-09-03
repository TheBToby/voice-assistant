import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from config import AgentSettings, _parse_mcp_servers_json

import json

import pytest


def base_env() -> dict:
    return {
        "LIVEKIT_URL": "ws://localhost:7880",
        "LIVEKIT_API_KEY": "devkey",
        "LIVEKIT_API_SECRET": "secret",
        "ELEVEN_API_KEY": "eleven",
        "OPENAI_API_KEY": "openai",
    }


def test_defaults():
    s = AgentSettings.from_env(base_env())
    assert s.llm_model == "gpt-4.1-mini"
    assert s.stt_model == "scribe_v2_realtime"
    assert s.tts_model == "eleven_turbo_v2_5"
    assert s.assistant_name == "Atlas"
    assert s.enable_turn_detector is True
    assert "Atlas" in s.instructions  # default prompt is templated


def test_valid_config_has_no_problems():
    assert AgentSettings.from_env(base_env()).validate() == []


def test_missing_keys_reported():
    s = AgentSettings.from_env(
        {"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": ""}
    )
    problems = s.validate()
    assert any("LIVEKIT_API_SECRET" in p for p in problems)
    assert any("ELEVEN_API_KEY" in p for p in problems)
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_home_assistant_mcp_url_and_headers():
    env = base_env() | {
        "HOME_ASSISTANT_URL": "http://homeassistant.local:8123/",
        "HOME_ASSISTANT_TOKEN": "eyJhbGc",
    }
    s = AgentSettings.from_env(env)
    servers = {x.id: x for x in s.mcp_servers()}
    ha = servers["home-assistant"]
    assert ha.url == "http://homeassistant.local:8123/api/mcp"  # trailing / stripped
    assert ha.headers == {"Authorization": "Bearer eyJhbGc"}


def test_home_assistant_ignored_without_token():
    env = base_env() | {"HOME_ASSISTANT_URL": "http://ha:8123"}
    servers = AgentSettings.from_env(env).mcp_servers()
    assert all(s.id != "home-assistant" for s in servers)


def test_weather_mcp_server_is_no_longer_bundled():
    """The bundled weather MCP server was removed: WEATHER_MCP_URL no longer
    registers anything. Weather comes from user-configured MCP_SERVERS_JSON."""
    env = base_env() | {"WEATHER_MCP_URL": "http://localhost:8100/mcp"}
    servers = AgentSettings.from_env(env).mcp_servers()
    assert all(s.id != "weather" for s in servers)


def test_weather_from_user_configured_mcp_json():
    env = base_env() | {
        "MCP_SERVERS_JSON": json.dumps(
            [{"id": "weather", "url": "http://localhost:9000/mcp"}]
        )
    }
    servers = {x.id: x for x in AgentSettings.from_env(env).mcp_servers()}
    assert servers["weather"].url == "http://localhost:9000/mcp"


def test_language_defaults_to_german():
    s = AgentSettings.from_env(base_env())
    assert s.language == "de"
    assert s.language_name == "German"
    assert "German" in s.instructions  # default prompt answers in German


def test_language_override_and_normalization():
    assert AgentSettings.from_env(base_env() | {"LANGUAGE": "en"}).language == "en"
    assert AgentSettings.from_env(base_env() | {"LANGUAGE": "DE-de"}).language == "de"
    assert AgentSettings.from_env(base_env() | {"LANGUAGE": "German"}).language == "de"
    english = AgentSettings.from_env(base_env() | {"LANGUAGE": "en"})
    assert "English" in english.instructions


def test_unknown_language_is_kept_but_flagged():
    s = AgentSettings.from_env(base_env() | {"LANGUAGE": "xx"})
    assert s.language == "xx"
    assert any("LANGUAGE" in p for p in s.validate())


def test_extra_servers_from_json_and_dedup():
    env = base_env() | {
        "MCP_SERVERS_JSON": json.dumps(
            [
                {"id": "music", "url": "http://localhost:9000/mcp"},
                {
                    "id": "secure",
                    "url": "https://example.com/mcp",
                    "headers": {"X-API-Key": "abc"},
                },
            ]
        )
    }
    servers = {x.id: x for x in AgentSettings.from_env(env).mcp_servers()}
    assert servers["music"].url == "http://localhost:9000/mcp"
    assert servers["secure"].headers == {"X-API-Key": "abc"}


def test_invalid_json_raises_helpful_error():
    with pytest.raises(ValueError) as excinfo:
        _parse_mcp_servers_json("{not json")
    assert "MCP_SERVERS_JSON" in str(excinfo.value)


def test_persona_overrides():
    env = base_env() | {
        "ASSISTANT_INSTRUCTIONS": "You are a pirate.",
        "ASSISTANT_NAME": "Jack",
    }
    s = AgentSettings.from_env(env)
    assert s.instructions == "You are a pirate."
    assert s.assistant_name == "Jack"
