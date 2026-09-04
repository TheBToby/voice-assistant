"""Settings model for the web console.

Pure logic (no FastAPI imports) so it can be unit-tested on the host, exactly
like agent/config.py.

Environment variables remain the *defaults*; the console stores per-key
overrides in SQLite and exposes the *effective* configuration (env value if
no override exists). Fundamental, one-time basic settings (LiveKit/Provider
credentials, authentication, ports, data locations) stay environment-only and
are surfaced read-only in the UI - see ENV_ONLY_FIELDS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

KIND_STR = "str"
KIND_TEXT = "text"
KIND_BOOL = "bool"
KIND_INT = "int"
KIND_SECRET = "secret"

# client/server sentinels for write-only secret fields
SENTINEL_SET = "__SET__"
SENTINEL_UNSET = "__UNSET__"

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class SettingDef:
    """One user-editable setting: env var is the default, DB the override."""

    key: str
    env_var: str
    default: str
    kind: str
    label: str
    group: str
    help: str = ""
    choices: tuple[str, ...] = ()


SETTING_DEFS: tuple[SettingDef, ...] = (
    # --- persona & conversation ------------------------------------------
    SettingDef(
        "assistant_name", "ASSISTANT_NAME", "Atlas", KIND_STR,
        "Assistant name", "Persona",
        "Used in the default system prompt.",
    ),
    SettingDef(
        "language", "LANGUAGE", "de", KIND_STR,
        "Language", "Persona",
        "Spoken replies, STT/TTS hint, built-in skills and turn detector.",
        choices=("de", "en"),
    ),
    SettingDef(
        "assistant_instructions", "ASSISTANT_INSTRUCTIONS", "", KIND_TEXT,
        "System prompt override", "Persona",
        "Full override of the default prompt. Empty = built-in default.",
    ),
    SettingDef(
        "greeting", "GREETING", "Sprachassistent bereit.", KIND_TEXT,
        "Greeting", "Persona",
        "Spoken when a session starts. Empty = silent.",
    ),
    SettingDef(
        "enable_turn_detector", "ENABLE_TURN_DETECTOR", "true", KIND_BOOL,
        "Turn detector", "Persona",
        "Model-based end-of-turn detection; model auto-picked from language.",
    ),
    SettingDef(
        "default_location", "DEFAULT_LOCATION", "", KIND_STR,
        "Default location", "Persona",
        "Used for weather queries when the user does not name a city.",
    ),
    SettingDef(
        "weather_units", "WEATHER_UNITS", "metric", KIND_STR,
        "Weather units", "Persona", "",
        choices=("metric", "imperial"),
    ),
    # --- speech & LLM ------------------------------------------------------
    SettingDef(
        "stt_model", "STT_MODEL", "scribe_v2_realtime", KIND_STR,
        "STT model", "Speech & LLM",
        "ElevenLabs STT model (scribe_v2_realtime = streaming).",
    ),
    SettingDef(
        "tts_model", "TTS_MODEL", "eleven_turbo_v2_5", KIND_STR,
        "TTS model", "Speech & LLM",
        "ElevenLabs TTS model (e.g. eleven_turbo_v2_5, eleven_flash_v2_5).",
    ),
    SettingDef(
        "tts_voice_id", "TTS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb", KIND_STR,
        "TTS voice ID", "Speech & LLM",
        "Voice from your ElevenLabs library.",
    ),
    SettingDef(
        "llm_model", "LLM_MODEL", "gpt-4.1-mini", KIND_STR,
        "LLM model", "Speech & LLM",
        "Any OpenAI-compatible model name.",
    ),
    SettingDef(
        "openai_base_url", "OPENAI_BASE_URL", "", KIND_STR,
        "LLM base URL", "Speech & LLM",
        "OpenAI-compatible endpoint (Ollama, vLLM, LM Studio). Empty = OpenAI.",
    ),
    # --- integrations -------------------------------------------------------
    SettingDef(
        "home_assistant_url", "HOME_ASSISTANT_URL", "", KIND_STR,
        "Home Assistant URL", "Integrations",
        "e.g. http://homeassistant.local:8123 (/api/mcp is appended).",
    ),
    SettingDef(
        "home_assistant_token", "HOME_ASSISTANT_TOKEN", "", KIND_SECRET,
        "Home Assistant token", "Integrations",
        "Long-lived access token. Write-only: the UI shows only whether it is set.",
    ),
    # --- devices & tokens ---------------------------------------------------
    SettingDef(
        "room_name", "ROOM_NAME", "home", KIND_STR,
        "Default room", "Devices & tokens",
        "LiveKit room used when minting tokens from the console.",
    ),
    SettingDef(
        "token_valid_hours", "TOKEN_VALID_HOURS", "12", KIND_INT,
        "Token validity (hours)", "Devices & tokens",
        "Lifetime of access tokens minted in the console.",
    ),
    # --- diagnostics ---------------------------------------------------------
    SettingDef(
        "diagnostics_history_days", "DIAGNOSTICS_HISTORY_DAYS", "30", KIND_INT,
        "Diagnostics history (days)", "Diagnostics",
        "Audit events older than this are deleted automatically.",
    ),
    SettingDef(
        "transcripts_enabled", "TRANSCRIPTS_ENABLED", "false", KIND_BOOL,
        "Store transcripts", "Diagnostics",
        "When off, only interaction metadata is recorded - never speech content.",
    ),
)

SETTABLE_KEYS: set[str] = {d.key for d in SETTING_DEFS}

# subset pushed to the agent via /internal/config
AGENT_SETTING_KEYS: tuple[str, ...] = (
    "assistant_name",
    "language",
    "assistant_instructions",
    "greeting",
    "enable_turn_detector",
    "default_location",
    "weather_units",
    "stt_model",
    "tts_model",
    "tts_voice_id",
    "llm_model",
    "openai_base_url",
    "home_assistant_url",
    "home_assistant_token",
)

# environment-only fields displayed read-only in the UI (fundamental settings)
ENV_ONLY_FIELDS: tuple[tuple[str, str, str], ...] = (
    # (label, env var, kind: "url"|"secret")
    ("LiveKit URL (agent)", "LIVEKIT_URL", "url"),
    ("LiveKit URL (devices)", "PUBLIC_LIVEKIT_WS_URL", "url"),
    ("LiveKit API key", "LIVEKIT_API_KEY", "secret"),
    ("LiveKit API secret", "LIVEKIT_API_SECRET", "secret"),
    ("ElevenLabs API key", "ELEVEN_API_KEY", "secret"),
    ("OpenAI API key", "OPENAI_API_KEY", "secret"),
)

MCP_SERVERS_KEY = "mcp_servers"  # settings-table key holding the UI-managed list


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _def(key: str) -> SettingDef | None:
    for d in SETTING_DEFS:
        if d.key == key:
            return d
    return None


def parse_bool(value: object, default: bool | None = False) -> bool | None:
    """Parse a bool from common string forms; `default` when unparseable."""
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else "").strip().lower()
    if raw in TRUE_WORDS:
        return True
    if raw in FALSE_WORDS:
        return False
    return default


LANGUAGE_ALIASES = {"german": "de", "deutsch": "de", "english": "en"}


def normalize_language(raw: str) -> str:
    """Mirror agent/i18n.normalize_language: 'de_DE.UTF-8' -> 'de', names too."""
    value = (raw or "").strip().lower()
    if value in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[value]
    return value.split("-")[0].split("_")[0].split(".")[0].strip()


def env_defaults(env: dict) -> dict[str, str]:
    """Effective defaults: env value when set, else the built-in default."""
    defaults: dict[str, str] = {}
    for d in SETTING_DEFS:
        raw = str(env.get(d.env_var, "") or "").strip()
        defaults[d.key] = raw if raw else d.default
    return defaults


def effective(env: dict, stored: dict[str, str]) -> dict[str, str]:
    """Merge env defaults with stored overrides (stored wins)."""
    values = env_defaults(env)
    for key, value in (stored or {}).items():
        if key in SETTABLE_KEYS and value is not None:
            values[key] = str(value)
    if values.get("language"):
        values["language"] = normalize_language(values["language"])
    return values
def validate_updates(updates: dict[str, str], env: dict | None = None) -> list[str]:
    """Return human-readable problems; empty list = safe to store."""
    problems: list[str] = []
    for key, raw in (updates or {}).items():
        d = _def(key)
        if d is None:
            problems.append(f"unknown setting: {key}")
            continue
        value = str(raw)
        if d.kind == KIND_INT:
            try:
                number = int(value.strip())
            except ValueError:
                problems.append(f"{d.label}: not a number")
                continue
            if key == "token_valid_hours" and not 1 <= number <= 24 * 31:
                problems.append("Token validity must be between 1 and 744 hours")
            if key == "diagnostics_history_days" and not 1 <= number <= 3650:
                problems.append("Diagnostics history must be between 1 and 3650 days")
        elif d.kind == KIND_BOOL:
            if parse_bool(value, None) is None:
                problems.append(f"{d.label}: must be true or false")
        elif d.choices and value.strip() not in d.choices:
            problems.append(f"{d.label}: must be one of {', '.join(d.choices)}")

    cross = effective(env or {}, updates or {})
    if bool(cross.get("home_assistant_url")) != bool(cross.get("home_assistant_token")):
        problems.append("Home Assistant: URL and token must be configured together")
    return problems


def sanitize_updates(updates: dict) -> tuple[dict[str, str], list[str]]:
    """Turn a client payload into storable values, honoring secret sentinels.

    Returns (updates_to_store, skipped_keys). Secrets are only stored when a
    new value is supplied; "" or __SET__ keeps the current one, __UNSET__
    clears it.
    """
    clean: dict[str, str] = {}
    skipped: list[str] = []
    for key, value in (updates or {}).items():
        d = _def(key)
        if d is None:
            skipped.append(key)
            continue
        raw = "" if value is None else str(value)
        if d.kind == KIND_SECRET:
            if raw in ("", SENTINEL_SET):
                skipped.append(key)
                continue
        if raw == SENTINEL_UNSET:
            clean[key] = ""  # universal "clear this override"
            continue
        clean[key] = raw if d.kind == KIND_TEXT else raw.strip()
    return clean, skipped


def redact_for_client(env: dict, stored: dict[str, str]) -> list[dict]:
    """Setting descriptors for the UI: effective value (secrets masked)."""
    values = effective(env, stored)
    out: list[dict] = []
    for d in SETTING_DEFS:
        value = values.get(d.key, "")
        if d.kind == KIND_SECRET:
            value = SENTINEL_SET if value else ""
        out.append(
            {
                "key": d.key,
                "label": d.label,
                "help": d.help,
                "kind": d.kind,
                "group": d.group,
                "choices": list(d.choices),
                "value": value,
                "stored": d.key in (stored or {}),
                "source": "ui" if d.key in (stored or {}) else "env",
            }
        )
    return out


def env_only_status(env: dict) -> list[dict]:
    """Read-only view of the fundamental env-only settings."""
    out: list[dict] = []
    for label, env_var, kind in ENV_ONLY_FIELDS:
        raw = str(env.get(env_var, "") or "")
        out.append(
            {
                "label": label,
                "env_var": env_var,
                "kind": kind,
                "value": raw if kind != "secret" else (SENTINEL_SET if raw else ""),
            }
        )
    return out
# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------
def parse_mcp_json(raw: str) -> list[dict]:
    """Parse MCP_SERVERS_JSON-style text: [{"id","url","headers"}]."""
    raw = (raw or "").strip()
    if not raw:
        return []
    entries = json.loads(raw)  # ValueError/TypeError handled by the caller
    if not isinstance(entries, list):
        raise ValueError("must be a JSON list of objects")
    specs: list[dict] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"entry {i} must be an object with 'url'")
        specs.append(
            {
                "id": str(entry.get("id") or f"mcp-{i}"),
                "url": str(entry["url"]),
                "headers": {
                    str(k): str(v) for k, v in (entry.get("headers") or {}).items()
                },
            }
        )
    return specs


def env_mcp_servers(env: dict) -> list[dict]:
    """MCP servers from MCP_SERVERS_JSON (empty list when unset/invalid)."""
    raw = str(env.get("MCP_SERVERS_JSON", "") or "")
    if not raw.strip():
        return []
    try:
        return parse_mcp_json(raw)
    except (ValueError, TypeError):
        return []


def normalize_ui_mcp_list(raw: object) -> list[dict]:
    """Validate/normalize a UI-supplied MCP server list.

    Each entry: {"id", "url", "headers"?, "enabled"?}. Raises ValueError on
    structural problems so the API can answer 400.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("mcp_servers must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or not str(entry.get("url", "")).strip():
            raise ValueError(f"mcp_servers[{i}] needs a 'url'")
        server_id = str(entry.get("id") or "").strip() or f"mcp-{i + 1}"
        if server_id in seen:
            raise ValueError(f"mcp_servers: duplicate id '{server_id}'")
        if any(c in server_id for c in ' "\'\t\r\n'):
            raise ValueError(f"mcp_servers: invalid id '{server_id}'")
        seen.add(server_id)
        headers = entry.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError(f"mcp_servers[{i}] headers must be an object")
        out.append(
            {
                "id": server_id,
                "url": str(entry["url"]).strip(),
                "headers": {str(k): str(v) for k, v in headers.items()},
                "enabled": parse_bool(entry.get("enabled", True), True),
            }
        )
    return out


def mask_secret(value: str) -> str:
    """Mask credential-like header values for display."""
    if not value:
        return ""
    if value.lower().startswith("bearer ") and len(value) > 10:
        return value[:8] + "***"
    if len(value) > 6:
        return value[:3] + "***"
    return "***"


def merged_mcp_view(
    ui_list: list[dict], env_list: list[dict], env: dict
) -> list[dict]:
    """Display merge for the UI (first definition per id wins, like the agent).

    Order: UI-managed entries, then MCP_SERVERS_JSON entries, then the Home
    Assistant integration. Shadowed entries are listed but flagged inactive.
    """
    values = effective(env, {})
    ha_url = values.get("home_assistant_url", "")
    ha_token = values.get("home_assistant_token", "")

    view: list[dict] = []
    seen: set[str] = set()

    def add(server_id: str, url: str, headers: dict, source: str, active: bool) -> None:
        view.append(
            {
                "id": server_id,
                "url": url,
                "headers": {k: mask_secret(v) for k, v in headers.items()},
                "source": source,
                "active": bool(active) and server_id not in seen,
            }
        )
        seen.add(server_id)

    for entry in ui_list or []:
        add(
            entry["id"],
            entry["url"],
            entry.get("headers", {}),
            "ui",
            bool(entry.get("enabled", True)),
        )
    for entry in env_list or []:
        add(entry["id"], entry["url"], entry.get("headers", {}), "env", True)
    if ha_url and ha_token:
        add(
            "home-assistant",
            ha_url.rstrip("/") + "/api/mcp",
            {"Authorization": f"Bearer {ha_token}"},
            "home-assistant",
            True,
        )
    return view


def agent_runtime_payload(
    env: dict, stored: dict[str, str], ui_mcp_list: list[dict], version: int
) -> dict:
    """Payload for GET /internal/config: what the agent applies per session."""
    values = effective(env, stored)
    return {
        "version": version,
        "settings": {key: values.get(key, "") for key in AGENT_SETTING_KEYS},
        "transcripts_enabled": parse_bool(values.get("transcripts_enabled"), False),
        "mcp_servers": [
            {
                "id": entry["id"],
                "url": entry["url"],
                "headers": entry.get("headers", {}),
            }
            for entry in (ui_mcp_list or [])
            if entry.get("enabled", True)
        ],
    }



