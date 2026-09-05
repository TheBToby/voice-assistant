"""Environment-driven configuration for the voice assistant agent.

This module is intentionally free of livekit imports so it can be unit-tested
without the heavy runtime (see tests/unit/test_config.py).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields, replace

from i18n import DEFAULT_LANGUAGE, PACKS, normalize_language
from i18n import language_name as human_language_name

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are {name}, a friendly and efficient home voice assistant.

# Voice basics
- Your replies are spoken out loud. Keep them SHORT and conversational
  (1-3 sentences unless the user asks for details). No markdown, no lists,
  no emojis.
- If a request is ambiguous, ask a single short clarifying question.
- Confirm completed actions in one brief sentence.

# Language
- Always answer in {language_name}, no matter which language the user speaks.
- Format numbers, dates, times and durations naturally for {language_name}.

# Built-in skills
- Time & date: use the get_current_time tool whenever the user asks about
  the time or date. Never guess the time.
- Timers: use set_timer for requests like "set a timer for 5 minutes",
  cancel_timer to cancel by name, list_timers to report running timers.
  Timers announce themselves when they expire. Confirm each timer with a
  short sentence.

# Weather
- Weather data comes from the weather MCP server the user configured
  (tools such as get_current_weather or get_weather_forecast). The user's
  location is {default_location}. When it is empty or the user asks about
  another place, ask which city they mean before calling the tool.
  Temperatures are in {weather_units} units.

# Home control
- Home Assistant tools from the home-assistant MCP server let you control
  and query smart-home devices (lights, switches, climate, media, sensors).
  Use them for any request like "turn off the kitchen light" or
  "is the front door locked?". Only entities exposed to assistants in
  Home Assistant are available.
- If a tool call fails or a device is unavailable, say so briefly and move on.

# Style
- You have a calm, helpful personality - like a well-designed smart speaker.
- Do not invent capabilities: if you have no tool for something, say so.
"""


@dataclass(frozen=True)
class MCPServerSpec:
    """A remote MCP server the agent should load tools from."""

    id: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSettings:
    # LiveKit
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    # Providers
    eleven_api_key: str
    openai_api_key: str
    openai_base_url: str
    llm_model: str
    stt_model: str
    tts_model: str
    tts_voice_id: str
    # Persona / skills
    assistant_name: str
    default_location: str
    weather_units: str
    instructions: str
    greeting: str
    enable_turn_detector: bool
    language: str
    # MCP
    home_assistant_url: str
    home_assistant_token: str
    mcp_servers_json: str
    # Web console (configuration & diagnostics UI); empty = console unused
    console_url: str = ""
    console_token: str = ""
    audit_enabled: bool = True
    # runtime-only (never set from env): comes from the console's
    # "Store transcripts" setting via apply_overrides()
    transcripts_enabled: bool = False
    # MCP servers managed in the web console; first definition per id wins,
    # so these shadow MCP_SERVERS_JSON entries and Home Assistant
    extra_mcp_specs: tuple[MCPServerSpec, ...] = ()

    # ------------------------------------------------------------------
    @property
    def language_name(self) -> str:
        """Human-readable configured language, e.g. 'de' -> 'German'."""
        return human_language_name(self.language)

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AgentSettings":
        e = env if env is not None else dict(os.environ)
        raw_language = e.get("LANGUAGE", DEFAULT_LANGUAGE)
        language = (
            normalize_language(raw_language)
            or raw_language.strip().lower()
            or DEFAULT_LANGUAGE
        )
        instructions = e.get("ASSISTANT_INSTRUCTIONS") or ""
        if not instructions:
            instructions = DEFAULT_SYSTEM_PROMPT.format(
                name=e.get("ASSISTANT_NAME", "Atlas"),
                default_location=e.get("DEFAULT_LOCATION", "")
                or "not configured - ask the user for their city",
                weather_units=e.get("WEATHER_UNITS", "metric"),
                language_name=human_language_name(language),
            )
        return cls(
            livekit_url=e.get("LIVEKIT_URL", "ws://localhost:7880"),
            livekit_api_key=e.get("LIVEKIT_API_KEY", "devkey"),
            livekit_api_secret=e.get("LIVEKIT_API_SECRET", ""),
            eleven_api_key=e.get("ELEVEN_API_KEY", ""),
            openai_api_key=e.get("OPENAI_API_KEY", ""),
            openai_base_url=e.get("OPENAI_BASE_URL", ""),
            llm_model=e.get("LLM_MODEL", "gpt-4.1-mini"),
            stt_model=e.get("STT_MODEL", "scribe_v2_realtime"),
            tts_model=e.get("TTS_MODEL", "eleven_turbo_v2_5"),
            tts_voice_id=e.get("TTS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"),
            assistant_name=e.get("ASSISTANT_NAME", "Atlas"),
            default_location=e.get("DEFAULT_LOCATION", ""),
            weather_units=e.get("WEATHER_UNITS", "metric"),
            instructions=instructions,
            greeting=e.get("GREETING", ""),
            enable_turn_detector=_to_bool(e.get("ENABLE_TURN_DETECTOR", "true")),
            language=language,
            home_assistant_url=e.get("HOME_ASSISTANT_URL", "").rstrip("/"),
            home_assistant_token=e.get("HOME_ASSISTANT_TOKEN", ""),
            mcp_servers_json=e.get("MCP_SERVERS_JSON", ""),
            console_url=_console_url(e),
            console_token=_internal_token(e),
            audit_enabled=not _to_bool(e.get("CONSOLE_AUDIT_DISABLED", "")),
        )

    # ------------------------------------------------------------------
    def mcp_servers(self) -> list[MCPServerSpec]:
        """Resolve all configured MCP servers (console + HA + generic JSON).

        Order matters: the first definition per id wins, so console-managed
        servers shadow MCP_SERVERS_JSON entries and Home Assistant.
        """
        servers: list[MCPServerSpec] = list(self.extra_mcp_specs)

        if self.home_assistant_url and self.home_assistant_token:
            # Official Home Assistant "Model Context Protocol Server"
            # integration: streamable HTTP at /api/mcp, Bearer auth with a
            # long-lived access token.
            servers.append(
                MCPServerSpec(
                    id="home-assistant",
                    url=f"{self.home_assistant_url}/api/mcp",
                    headers={
                        "Authorization": f"Bearer {self.home_assistant_token}"
                    },
                )
            )

        servers.extend(_parse_mcp_servers_json(self.mcp_servers_json))

        # de-duplicate by id, first definition wins
        seen: set[str] = set()
        unique: list[MCPServerSpec] = []
        for srv in servers:
            if srv.id in seen:
                continue
            seen.add(srv.id)
            unique.append(srv)
        return unique

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems."""
        problems: list[str] = []
        if not self.livekit_api_secret:
            problems.append("LIVEKIT_API_SECRET is empty")
        if not self.eleven_api_key:
            problems.append("ELEVEN_API_KEY is empty (required for STT/TTS)")
        if not self.openai_api_key and not self.openai_base_url:
            problems.append(
                "OPENAI_API_KEY is empty (or set OPENAI_BASE_URL for a local LLM)"
            )
        if self.language not in PACKS:
            problems.append(
                f"LANGUAGE={self.language} has no built-in skill translations; "
                "built-in replies fall back to English"
            )
        return problems


# ----------------------------------------------------------------------
def _to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _console_url(env: dict) -> str:
    """Base URL of the web console (empty disables audit/runtime config)."""
    explicit = (env.get("CONSOLE_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    # default: the console service in the same stack (host networking)
    port = (env.get("UI_PORT", "") or "").strip() or "8090"
    return f"http://localhost:{port}"


def _internal_token(env: dict) -> str:
    """Shared secret for the console's /internal API.

    Same derivation as the console (ui/app/auth_core.py): an explicit
    CONSOLE_INTERNAL_TOKEN wins, otherwise the value is derived from
    LIVEKIT_API_SECRET, which agent and console both know.
    """
    explicit = (env.get("CONSOLE_INTERNAL_TOKEN", "") or "").strip()
    if explicit:
        return explicit
    secret = env.get("LIVEKIT_API_SECRET", "") or ""
    if not secret:
        return ""
    import hashlib
    import hmac as _hmac

    return _hmac.new(
        secret.encode(), b"voice-assistant/console-internal", hashlib.sha256
    ).hexdigest()[:40]


# settings the console may override; keys are the console's setting names
# (ui/app/settings_core.py SETTING_DEFS), mapped onto AgentSettings fields
# via _CONSOLE_KEY_ALIASES where the names differ
_CONSOLE_SETTING_FIELDS: tuple[str, ...] = (
    "assistant_name",
    "language",
    "assistant_instructions",  # console key; AgentSettings field: instructions
    "greeting",
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

# console setting key -> AgentSettings field, for keys whose names differ
_CONSOLE_KEY_ALIASES: dict[str, str] = {
    "assistant_instructions": "instructions",
}


def apply_overrides(base: AgentSettings, payload: dict) -> AgentSettings:
    """Merge the console's GET /internal/config payload into the settings.

    Pure function: returns a new AgentSettings (frozen dataclass). Unknown
    keys are ignored; a malformed payload yields the env-built base so the
    assistant keeps working when the console sends garbage.
    """
    if not isinstance(payload, dict):
        return base
    values = payload.get("settings")
    if not isinstance(values, dict):
        values = {}

    updates: dict[str, object] = {}
    for key in _CONSOLE_SETTING_FIELDS:
        value = values.get(key)
        if value is None:
            continue
        name = _CONSOLE_KEY_ALIASES.get(key, key)
        text = str(value)
        if key == "language":
            # console values arrive pre-normalized, but stay defensive
            text = normalize_language(text)
        if name == "instructions" and not text.strip():
            # console "" = "use the built-in default prompt" - same semantics
            # as an unset ASSISTANT_INSTRUCTIONS in from_env()
            continue
        updates[name] = text
    if values.get("enable_turn_detector") is not None:
        updates["enable_turn_detector"] = _to_bool(
            str(values["enable_turn_detector"])
        )
    if "transcripts_enabled" in payload:
        updates["transcripts_enabled"] = bool(payload.get("transcripts_enabled"))

    specs: list[MCPServerSpec] = []
    for entry in payload.get("mcp_servers") or []:
        try:
            specs.append(
                MCPServerSpec(
                    id=str(entry["id"]),
                    url=str(entry["url"]),
                    headers={
                        str(k): str(v)
                        for k, v in (entry.get("headers") or {}).items()
                    },
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if specs:
        updates["extra_mcp_specs"] = tuple(specs)

    # safety net: a console key that does not match an AgentSettings field
    # (ui/ and agent/ deployments drifting apart) must never crash the job
    known_fields = {f.name for f in fields(base)}
    for key in sorted(set(updates) - known_fields):
        logger.warning("ignoring console setting %r: unknown agent field", key)
        del updates[key]

    if not updates:
        return base
    return replace(base, **updates)


def _parse_mcp_servers_json(raw: str) -> list[MCPServerSpec]:
    """Parse MCP_SERVERS_JSON: [{"id": "...", "url": "...", "headers": {..}}]."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"MCP_SERVERS_JSON is not valid JSON: {exc}. "
            'Expected format: [{{"id":"x","url":"http://...","headers":{{}}}}]'
        ) from exc
    if not isinstance(entries, list):
        raise ValueError("MCP_SERVERS_JSON must be a JSON list of objects")

    specs: list[MCPServerSpec] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"MCP_SERVERS_JSON[{i}] must be an object with 'url'")
        spec = MCPServerSpec(
            id=str(entry.get("id") or f"mcp-{i}"),
            url=str(entry["url"]),
            headers={
                str(k): str(v) for k, v in (entry.get("headers") or {}).items()
            },
        )
        specs.append(spec)
    return specs

