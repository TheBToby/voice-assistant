"""Agent worker entrypoint.

Run modes (livekit-agents CLI):
    python main.py dev      # local development, verbose
    python main.py start    # production mode (used by docker-compose)
    python main.py console  # terminal-based test client (needs local audio)

The worker auto-dispatches into every new LiveKit room, so any participant
connecting with a token (ESP32 device, browser test client, smoke test)
immediately gets a voice assistant.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli, mcp
from livekit.plugins import openai, silero
from livekit.plugins import elevenlabs

import audit as audit_module
from assistant import Assistant
from config import AgentSettings, apply_overrides
from timers import TimerService

logger = logging.getLogger("voice-assistant")


def build_mcp_toolsets(settings: AgentSettings) -> list[mcp.MCPToolset]:
    """Wrap every configured MCP server in an MCPToolset."""
    toolsets: list[mcp.MCPToolset] = []
    for spec in settings.mcp_servers():
        source = "console" if spec in settings.extra_mcp_specs else "env"
        logger.info("MCP server registered: %s -> %s (%s)", spec.id, spec.url, source)
        toolsets.append(
            mcp.MCPToolset(
                id=spec.id,
                mcp_server=mcp.MCPServerHTTP(spec.url, headers=spec.headers or None),
            )
        )
    return toolsets


async def load_runtime_settings(base: AgentSettings) -> AgentSettings:
    """Fetch effective settings from the web console (best effort).

    The console merges its DB overrides with the env defaults, so settings
    changed in the UI apply to the next session without a restart. When the
    console is unreachable the env-only configuration is used.
    """
    if not base.console_url or not base.console_token:
        return base
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{base.console_url}/internal/config",
                headers={"Authorization": f"Bearer {base.console_token}"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:  # noqa: BLE001 - env config remains the fallback
        logger.info(
            "console not reachable at %s; using environment configuration",
            base.console_url,
        )
        return base
    settings = apply_overrides(base, payload)
    logger.info(
        "runtime configuration loaded from console (config version %s)",
        payload.get("version", "?"),
    )
    return settings


def build_session(settings: AgentSettings) -> AgentSession:
    """Create the voice pipeline: ElevenLabs STT/TTS + configurable LLM."""
    import inspect

    # livekit-agents >= 1.6 uses `model=`, 1.5.x uses `model_id=`; both enable
    # realtime streaming when the model is scribe_v2_realtime.
    stt_sig = inspect.signature(elevenlabs.STT.__init__).parameters
    stt_kwargs: dict = {}
    if "model" in stt_sig:
        stt_kwargs["model"] = settings.stt_model
    elif "model_id" in stt_sig:
        stt_kwargs["model_id"] = settings.stt_model
    else:  # pragma: no cover - future plugin changes
        stt_kwargs["model"] = settings.stt_model
    # language hint: `language_code` on livekit-agents 1.5.x, `language` on
    # newer plugin versions
    if "language_code" in stt_sig:
        stt_kwargs["language_code"] = settings.language
    elif "language" in stt_sig:
        stt_kwargs["language"] = settings.language
    stt = elevenlabs.STT(**stt_kwargs)

    tts_kwargs: dict = {"model": settings.tts_model, "voice_id": settings.tts_voice_id}
    # `language` improves pronunciation/text normalization; only the
    # eleven_turbo_v2_5 model family accepts it
    tts_sig = inspect.signature(elevenlabs.TTS.__init__).parameters
    if "language" in tts_sig and "v2_5" in settings.tts_model:
        tts_kwargs["language"] = settings.language
    tts = elevenlabs.TTS(**tts_kwargs)

    llm_kwargs: dict = {"model": settings.llm_model}
    if settings.openai_base_url:
        # OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, OpenRouter, ...)
        llm_kwargs["base_url"] = settings.openai_base_url
        llm_kwargs["api_key"] = settings.openai_api_key or "not-needed"
    llm = openai.LLM(**llm_kwargs)

    turn_detection = None
    if settings.enable_turn_detector:
        turn_detection = _build_turn_detector(settings)

    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=silero.VAD.load(),
        turn_detection=turn_detection,
        preemptive_generation=True,  # start TTS before the LLM finishes -> lower latency
    )


def _build_turn_detector(settings: AgentSettings):
    """Pick the turn detector matching the configured language.

    English uses the dedicated English model, German the multilingual model
    (both ship with the turn-detector extra; the multilingual model also
    understands more languages if ever needed). Any other language falls
    back to VAD endpointing. Set ENABLE_TURN_DETECTOR=false to skip the
    model downloads entirely.
    """
    lang = settings.language
    try:
        if lang == "de":
            from livekit.plugins.turn_detector.multilingual import MultilingualModel

            return MultilingualModel()
        if lang == "en":
            from livekit.plugins.turn_detector.english import EnglishModel

            return EnglishModel()
    except Exception:  # noqa: BLE001 - fall back to STT endpointing
        logger.warning(
            "turn detector unavailable, falling back to VAD endpointing",
            exc_info=True,
        )
        return None
    logger.info(
        "no turn detector for language '%s'; using VAD endpointing", lang
    )
    return None


async def entrypoint(ctx: JobContext) -> None:
    base_settings = AgentSettings.from_env(os.environ)
    for problem in base_settings.validate():
        logger.warning("configuration: %s", problem)

    settings = await load_runtime_settings(base_settings)

    logger.info(
        "assistant language: %s (%s)", settings.language, settings.language_name
    )
    logger.info("joining room %s", ctx.room.name)
    await ctx.connect()

    # ------------------------------------------------------------------
    # diagnostics: audit reporter (best-effort, never blocks the pipeline)
    # ------------------------------------------------------------------
    reporter: audit_module.AuditReporter | None = None
    if settings.audit_enabled and settings.console_url and settings.console_token:
        reporter = audit_module.AuditReporter(
            console_url=settings.console_url,
            token=settings.console_token,
        )
        reporter.configure(
            room=ctx.room.name,
            agent_identity=ctx.room.local_participant.identity,
            transcripts=settings.transcripts_enabled,
        )
        reporter.event("agent.ready")
        for participant in ctx.room.remote_participants.values():
            reporter.event(
                "device.join",
                identity=participant.identity,
                name=participant.name,
            )
        reporter.start()

        def _on_participant_connected(participant) -> None:  # noqa: ANN001
            reporter.event(
                "device.join",
                identity=participant.identity,
                name=participant.name,
            )

        def _on_participant_disconnected(participant) -> None:  # noqa: ANN001
            reporter.event("device.leave", identity=participant.identity)

        ctx.room.on("participant_connected", _on_participant_connected)
        ctx.room.on("participant_disconnected", _on_participant_disconnected)

    async def _report_session_ended() -> None:
        if reporter is not None:
            reporter.event("session.ended")
            await reporter.aclose()

    try:
        ctx.add_shutdown_callback(_report_session_ended)
    except AttributeError:  # older/newer livekit-agents API
        logger.debug("add_shutdown_callback unavailable; ending events skipped")

    # ------------------------------------------------------------------
    timers = TimerService()
    session = build_session(settings)
    assistant = Assistant(
        settings, build_mcp_toolsets(settings), timers=timers, audit=reporter
    )
    assistant.bind_session(session)

    if reporter is not None:
        reporter.event(
            "session.started",
            data={
                "participants": [
                    {"identity": p.identity, "name": p.name}
                    for p in ctx.room.remote_participants.values()
                ],
            },
        )
        reporter.attach_session(session)

    await session.start(room=ctx.room, agent=assistant)

    if settings.greeting:
        await session.say(settings.greeting)
    logger.info("assistant ready in room %s", ctx.room.name)


def main() -> None:
    load_dotenv()
    level = os.getenv("LOG_LEVEL", "info").upper()
    logging.basicConfig(level=level)
    # quiet down noisy third-party loggers a bit
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
