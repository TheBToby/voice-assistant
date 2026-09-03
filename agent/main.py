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

import logging
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli, mcp
from livekit.plugins import openai, silero
from livekit.plugins import elevenlabs

from assistant import Assistant
from config import AgentSettings
from timers import TimerService

logger = logging.getLogger("voice-assistant")


def build_mcp_toolsets(settings: AgentSettings) -> list[mcp.MCPToolset]:
    """Wrap every configured MCP server in an MCPToolset."""
    toolsets: list[mcp.MCPToolset] = []
    for spec in settings.mcp_servers():
        logger.info("MCP server registered: %s -> %s", spec.id, spec.url)
        toolsets.append(
            mcp.MCPToolset(
                id=spec.id,
                mcp_server=mcp.MCPServerHTTP(spec.url, headers=spec.headers or None),
            )
        )
    return toolsets


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
    stt = elevenlabs.STT(**stt_kwargs)

    tts = elevenlabs.TTS(model=settings.tts_model, voice_id=settings.tts_voice_id)

    llm_kwargs: dict = {"model": settings.llm_model}
    if settings.openai_base_url:
        # OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, OpenRouter, ...)
        llm_kwargs["base_url"] = settings.openai_base_url
        llm_kwargs["api_key"] = settings.openai_api_key or "not-needed"
    llm = openai.LLM(**llm_kwargs)

    turn_detection = None
    if settings.enable_turn_detector:
        try:
            from livekit.plugins.turn_detector.english import EnglishModel

            turn_detection = EnglishModel()
        except Exception:  # noqa: BLE001 - fall back to STT endpointing
            logger.warning(
                "turn detector unavailable, falling back to VAD endpointing",
                exc_info=True,
            )

    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=silero.VAD.load(),
        turn_detection=turn_detection,
        preemptive_generation=True,  # start TTS before the LLM finishes -> lower latency
    )


async def entrypoint(ctx: JobContext) -> None:
    settings = AgentSettings.from_env()
    for problem in settings.validate():
        logger.warning("configuration: %s", problem)

    logger.info("joining room %s", ctx.room.name)
    await ctx.connect()

    timers = TimerService()
    session = build_session(settings)
    assistant = Assistant(settings, build_mcp_toolsets(settings), timers=timers)
    assistant.bind_session(session)

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
