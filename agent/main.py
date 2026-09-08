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
from collections.abc import Iterable

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli, mcp
from livekit.plugins import openai, silero
from livekit.plugins import elevenlabs

import audit as audit_module
import debug_audio
import log_filters
from assistant import Assistant
from config import AgentSettings, apply_overrides, mcp_transport_type
from timers import TimerService

logger = logging.getLogger("voice-assistant")


class _ToolFilteredMCPServerHTTP(mcp.MCPServerHTTP):
    """MCPServerHTTP that hides individual tools disabled in the console.

    livekit-agents fetches an MCP server's tools by calling ``list_tools()``
    on the server when the toolset is set up. Filtering here removes disabled
    tools before they ever reach the LLM (tools whose names cannot be
    determined are kept - the console toggle is name-based).
    """

    def __init__(
        self,
        url: str,
        headers: dict | None = None,
        disabled_tools: Iterable[str] = (),
        transport_type: str = "streamable_http",
    ) -> None:
        super().__init__(url, transport_type=transport_type, headers=headers)
        self._disabled_tools = frozenset(disabled_tools)

    async def list_tools(self, *args, **kwargs):  # noqa: ANN002, ANN003
        tools = await super().list_tools(*args, **kwargs)
        if not self._disabled_tools:
            return tools
        kept = [
            tool
            for tool in tools
            if getattr(getattr(tool, "info", None), "name", None)
            not in self._disabled_tools
        ]
        dropped = len(tools) - len(kept)
        if dropped:
            logger.info(
                "MCP server %s: %d tool(s) hidden (disabled in the console)",
                self.url,
                dropped,
            )
        return kept


def build_mcp_toolsets(settings: AgentSettings) -> list[mcp.MCPToolset]:
    """Wrap every configured MCP server in an MCPToolset."""
    toolsets: list[mcp.MCPToolset] = []
    for spec in settings.mcp_servers():
        source = "console" if spec in settings.extra_mcp_specs else "env"
        logger.info("MCP server registered: %s -> %s (%s)", spec.id, spec.url, source)
        toolsets.append(
            mcp.MCPToolset(
                id=spec.id,
                mcp_server=_ToolFilteredMCPServerHTTP(
                    spec.url,
                    headers=spec.headers or None,
                    disabled_tools=spec.disabled_tools,
                    # streamable HTTP unless the URL marks a legacy SSE
                    # endpoint - livekit-agents' own URL detection would
                    # otherwise pick SSE for every non-/mcp URL (e.g. Obot
                    # "mcp-connect" gateways -> HTTP 400)
                    transport_type=mcp_transport_type(spec.url, spec.transport),
                ),
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


_TURN_DETECTOR_OFF = ("0", "false", "no", "off")


def _prepare_turn_detector() -> None:
    """Register the turn-detector model runners in the main worker process.

    MUST run in the main process before ``cli.run_app()``: livekit-agents
    only spawns the dedicated inference process (hosting the ONNX
    end-of-utterance model) when at least one ``InferenceRunner`` has been
    registered there - and that happens when the plugin module is imported.
    Importing it from the job entrypoint (where ``build_session`` runs) is
    too late: the worker then has no inference executor and every
    end-of-turn prediction fails with "no inference executor", silently
    degrading turn detection to plain VAD endpointing.

    Both the English and the multilingual model are registered (~66 MB +
    ~396 MB q8 ONNX in the inference process), so the assistant language
    can also be switched at runtime via the console.

    The inference process opens the model files with ``local_files_only``
    - they are therefore pre-downloaded into the shared HF cache (the
    model-cache volume) here. Cached files make this a no-op, so offline
    restarts keep working. If the models cannot be prepared, the turn
    detector stays disabled for this worker run and sessions fall back to
    VAD endpointing instead of crash-looping the worker.
    """
    if os.getenv("ENABLE_TURN_DETECTOR", "true").strip().lower() in _TURN_DETECTOR_OFF:
        logger.info(
            "turn detector disabled (ENABLE_TURN_DETECTOR); using VAD endpointing"
        )
        return
    try:
        # importing the package registers the English + multilingual
        # InferenceRunners in this (main worker) process
        import livekit.plugins.turn_detector  # noqa: F401
        from livekit.plugins.turn_detector.english import _EUORunnerEn
        from livekit.plugins.turn_detector.models import HG_MODEL, ONNX_FILENAME
        from livekit.plugins.turn_detector.multilingual import (
            _EUORunnerMultilingual,
        )

        def _files_cached(runner_cls) -> bool:  # noqa: ANN001
            from huggingface_hub import hf_hub_download

            try:
                hf_hub_download(
                    HG_MODEL,
                    ONNX_FILENAME,
                    subfolder="onnx",
                    revision=runner_cls.model_revision(),
                    local_files_only=True,
                )
                hf_hub_download(
                    HG_MODEL,
                    "languages.json",
                    revision=runner_cls.model_revision(),
                    local_files_only=True,
                )
                return True
            except Exception:  # noqa: BLE001 - not cached yet
                return False

        for runner_cls in (_EUORunnerEn, _EUORunnerMultilingual):
            if not _files_cached(runner_cls):
                logger.info(
                    "downloading turn detector model (%s)...",
                    runner_cls.model_revision(),
                )
                runner_cls._download_files()  # noqa: SLF001 - pinned plugin API

        # job processes check this flag (inherited environment) and skip
        # the model entirely when it could not be made available
        os.environ["TURN_DETECTOR_AVAILABLE"] = "1"
        logger.info("turn detector models ready (english + multilingual)")
    except Exception:  # noqa: BLE001 - never block the worker on the EOU model
        logger.warning(
            "turn detector models could not be prepared - falling back to "
            "VAD endpointing (check network access to huggingface.co)",
            exc_info=True,
        )


def _build_turn_detector(settings: AgentSettings):
    """Pick the turn detector matching the configured language.

    English uses the dedicated English model, German the multilingual model
    (the multilingual model also understands more languages if ever
    needed). Any other language falls back to VAD endpointing.

    The models run in the worker's dedicated inference process, which only
    exists when `_prepare_turn_detector` registered the model runners in
    the main worker process (TURN_DETECTOR_AVAILABLE is set accordingly) -
    without it every end-of-turn prediction would fail with "no inference
    executor".
    """
    if not os.getenv("TURN_DETECTOR_AVAILABLE"):
        logger.info("turn detector not available; using VAD endpointing")
        return None
    lang = settings.language
    try:
        if lang == "de":
            from livekit.plugins.turn_detector.multilingual import MultilingualModel

            return MultilingualModel()
        if lang == "en":
            from livekit.plugins.turn_detector.english import EnglishModel

            return EnglishModel()
    except Exception:  # noqa: BLE001 - fall back to VAD endpointing
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

    # ------------------------------------------------------------------
    # diagnostics (temporary): record everything the agent HEARS to WAV
    # files so "is real voice audio arriving?" can be verified by
    # listening (DEBUG_RECORD_AUDIO=false turns this off; see
    # agent/debug_audio.py). Attached before connect() so the device's
    # already-published track is captured too.
    # ------------------------------------------------------------------
    recorder = None
    if settings.debug_record_audio:
        recorder = debug_audio.RemoteAudioRecorder()
        recorder.attach(ctx.room)

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

    if recorder is not None:
        # one-line breadcrumbs for the voice chain: VAD seeing speech and
        # STT producing text - together with the WAV recordings this pins
        # down where "no voice command recognized" breaks
        def _on_transcript(ev) -> None:  # noqa: ANN001
            if getattr(ev, "is_final", False):
                logger.info(
                    "audio debug: final user transcript: %r", ev.transcript
                )

        def _on_user_state(ev) -> None:  # noqa: ANN001
            if ev.new_state == "speaking":
                logger.info("audio debug: VAD: user started speaking")
            elif ev.new_state == "listening":
                logger.info("audio debug: VAD: user stopped speaking")

        session.on("user_input_transcribed", _on_transcript)
        session.on("user_state_changed", _on_user_state)

        async def _close_recorder() -> None:
            await recorder.aclose()

        try:
            ctx.add_shutdown_callback(_close_recorder)
        except AttributeError:  # older/newer livekit-agents API
            logger.debug("add_shutdown_callback unavailable; recorder not closed")

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
    # ElevenLabs STT websockets close with code 1000 (normal closure) when a
    # session ends; the plugin logs a full ERROR traceback and the framework
    # logs its automatic retry. That expected, self-healed noise is hidden at
    # info and higher, and logged as DEBUG with LOG_LEVEL=debug (see
    # log_filters.py).
    log_filters.install()

    # register the turn-detector runners + pre-download the model files in
    # THIS (main worker) process - see the docstring for why this must not
    # happen inside the job entrypoint
    _prepare_turn_detector()

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
