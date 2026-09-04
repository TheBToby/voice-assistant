"""Tests for the web console settings model (ui/app/settings_core.py)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui" / "app"))

import settings_core as sc


def base_env() -> dict:
    return {
        "LIVEKIT_API_KEY": "devkey",
        "LIVEKIT_API_SECRET": "secret",
        "ELEVEN_API_KEY": "eleven",
        "OPENAI_API_KEY": "openai",
    }


def test_env_defaults_and_builtin_fallback():
    env = base_env() | {"LANGUAGE": "en", "GREETING": "Hallo!"}
    values = sc.effective(env, {})
    assert values["language"] == "en"
    assert values["greeting"] == "Hallo!"
    assert values["assistant_name"] == "Atlas"  # built-in default
    assert values["diagnostics_history_days"] == "30"
    assert values["transcripts_enabled"] == "false"


def test_stored_overrides_win_over_env():
    env = base_env() | {"LANGUAGE": "en"}
    stored = {"language": "de", "llm_model": "llama3.1:8b"}
    values = sc.effective(env, stored)
    assert values["language"] == "de"
    assert values["llm_model"] == "llama3.1:8b"
    # unknown keys are ignored
    assert sc.effective(env, {"hacker": "1"})["assistant_name"] == "Atlas"


def test_validate_choices_and_ints():
    problems = sc.validate_updates(
        {
            "weather_units": "kelvin",
            "diagnostics_history_days": "0",
            "token_valid_hours": "abc",
            "transcripts_enabled": "maybe",
        },
        base_env(),
    )
    assert len(problems) == 4
    assert sc.validate_updates(
        {
            "weather_units": "imperial",
            "diagnostics_history_days": "90",
            "token_valid_hours": "24",
            "transcripts_enabled": "true",
        },
        base_env(),
    ) == []


def test_validate_unknown_key_and_ha_pairing():
    assert sc.validate_updates({"nope": "1"}, base_env())
    problems = sc.validate_updates(
        {"home_assistant_url": "http://ha:8123"}, base_env()
    )
    assert any("together" in p for p in problems)


def test_sanitize_updates_secret_sentinels():
    clean, skipped = sc.sanitize_updates(
        {
            "assistant_name": "  Atlas  ",
            "home_assistant_token": sc.SENTINEL_SET,  # keep current
            "home_assistant_url": sc.SENTINEL_UNSET,  # not a secret: stored as ""
            "unknown_key": "x",
        }
    )
    assert clean == {"assistant_name": "Atlas", "home_assistant_url": ""}
    assert skipped == ["home_assistant_token", "unknown_key"]
    # a real new secret value is stored
    clean, _ = sc.sanitize_updates({"home_assistant_token": "ey123"})
    assert clean == {"home_assistant_token": "ey123"}


def test_redact_masks_secret_and_reports_source():
    stored = {"home_assistant_token": "secret-token"}
    view = {s["key"]: s for s in sc.redact_for_client(base_env(), stored)}
    assert view["home_assistant_token"]["value"] == sc.SENTINEL_SET
    assert view["home_assistant_token"]["stored"] is True
    assert view["assistant_name"]["source"] == "env"
    assert view["assistant_name"]["value"] == "Atlas"


def test_env_only_status_masks_secrets():
    env = base_env() | {"LIVEKIT_URL": "ws://localhost:7880"}
    fields = {f["env_var"]: f for f in sc.env_only_status(env)}
    assert fields["LIVEKIT_API_SECRET"]["value"] == sc.SENTINEL_SET
    assert fields["LIVEKIT_URL"]["value"] == "ws://localhost:7880"


def test_normalize_ui_mcp_list():
    servers = sc.normalize_ui_mcp_list(
        [
            {"id": "weather", "url": "http://w:9000/mcp"},
            {"url": "http://x/mcp", "enabled": "false"},
        ]
    )
    assert servers[0] == {
        "id": "weather", "url": "http://w:9000/mcp", "headers": {}, "enabled": True,
    }
    assert servers[1]["id"] == "mcp-2"
    assert servers[1]["enabled"] is False

    import pytest

    with pytest.raises(ValueError):
        sc.normalize_ui_mcp_list([{"id": "a", "url": ""}])
    with pytest.raises(ValueError):
        sc.normalize_ui_mcp_list(
            [{"id": "a", "url": "http://a"}, {"id": "a", "url": "http://b"}]
        )
    with pytest.raises(ValueError):
        sc.normalize_ui_mcp_list("not-a-list")


def test_env_mcp_servers_and_merged_view():
    env = base_env() | {
        "MCP_SERVERS_JSON": json.dumps(
            [{"id": "weather", "url": "http://env-weather/mcp"}]
        ),
        "HOME_ASSISTANT_URL": "http://ha.local:8123/",
        "HOME_ASSISTANT_TOKEN": "ha-token",
    }
    ui_list = sc.normalize_ui_mcp_list(
        [{"id": "weather", "url": "http://ui-weather/mcp"},
         {"id": "music", "url": "http://music/mcp", "enabled": False}]
    )
    view = {}
    for server in sc.merged_mcp_view(ui_list, sc.env_mcp_servers(env), env):
        if server["active"]:
            view[server["id"]] = server
    # UI shadows the env entry with the same id (first definition wins)
    assert view["weather"]["url"] == "http://ui-weather/mcp"
    # the disabled music server is not active (but still listed)
    all_servers = sc.merged_mcp_view(ui_list, sc.env_mcp_servers(env), env)
    assert view["home-assistant"]["url"] == "http://ha.local:8123/api/mcp"
    # the shadowed env entry is listed, flagged inactive; disabled UI entry too
    flagged: dict = {}
    for s in all_servers:
        flagged.setdefault(s["id"], s["active"])  # first (winning) definition
    assert flagged == {"weather": True, "music": False, "home-assistant": True}
    # header values are masked for display
    assert view["home-assistant"]["headers"]["Authorization"].endswith("***")


def test_language_is_normalized_like_the_agent():
    env = {"LANGUAGE": "de_DE.UTF-8"}
    values = sc.effective(env, {})
    assert values["language"] == "de"
    values = sc.effective({}, {"language": "German"})
    assert values["language"] == "de"
    values = sc.effective({"LANGUAGE": "en-US"}, {})
    assert values["language"] == "en"
    assert sc.normalize_language("") == ""


def test_agent_runtime_payload():
    env = base_env() | {"LANGUAGE": "en"}
    stored = {"transcripts_enabled": "true"}
    ui_list = sc.normalize_ui_mcp_list([{"id": "weather", "url": "http://w/mcp"}])
    payload = sc.agent_runtime_payload(env, stored, ui_list, version=7)
    assert payload["version"] == 7
    assert payload["transcripts_enabled"] is True
    assert payload["settings"]["language"] == "en"
    assert payload["settings"]["llm_model"] == "gpt-4.1-mini"
    assert payload["mcp_servers"] == [
        {"id": "weather", "url": "http://w/mcp", "headers": {}}
    ]
