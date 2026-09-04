"""Connectivity probes for the console diagnostics.

Network calls (httpx + LiveKit server API) with the *interpretation* of the
results kept in small pure helpers so the interesting logic is testable.

MCP probe: speaks the streamable-HTTP transport of the Model Context
Protocol directly (initialize -> tools/list) with httpx, so no MCP SDK is
needed in the console image. SSE-style responses are parsed too, since
MCPServerHTTP in the agent accepts both transports.
"""

from __future__ import annotations

import json
import time

PROBE_TIMEOUT = 5.0
MCP_PROTOCOL_VERSION = "2024-11-05"
AGENT_HEARTBEAT_MAX_AGE = 120.0  # seconds before the agent counts as offline


# ---------------------------------------------------------------------------
# pure result helpers
# ---------------------------------------------------------------------------
def heartbeat_online(age_seconds: float | None) -> bool:
    return age_seconds is not None and age_seconds <= AGENT_HEARTBEAT_MAX_AGE


def summarize_probe(
    started: float, ok: bool, tools: list[str] | None = None, error: str = ""
) -> dict:
    return {
        "ok": ok,
        "latency_ms": int((time.time() - started) * 1000),
        "tools": tools or [],
        "error": error,
    }


def parse_mcp_response(text: str, content_type: str) -> dict:
    """Extract the JSON-RPC object from a JSON or SSE body. Raises ValueError."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    if "text/event-stream" in (content_type or ""):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    return json.loads(payload)
        raise ValueError("no data frame in SSE response")
    return json.loads(text)


def extract_tool_names(result: dict) -> list[str]:
    """Tool names from a tools/list result, tolerating shape differences."""
    tools = result.get("tools") if isinstance(result, dict) else None
    names: list[str] = []
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("name"):
            names.append(str(tool["name"]))
    return names


def ws_to_http(url: str) -> str:
    """LiveKit client URLs (ws://) to the HTTP API base."""
    if url.startswith("ws://"):
        return "http://" + url[5:]
    if url.startswith("wss://"):
        return "https://" + url[6:]
    return url


def provider_statuses(env: dict) -> list[dict]:
    """Configured-ness of the cloud providers (no network calls, no cost)."""
    return [
        {
            "name": "ElevenLabs (STT/TTS)",
            "configured": bool(str(env.get("ELEVEN_API_KEY", "") or "").strip()),
            "detail": "ELEVEN_API_KEY",
        },
        {
            "name": "LLM (OpenAI-compatible)",
            "configured": bool(
                str(env.get("OPENAI_API_KEY", "") or "").strip()
                or str(env.get("OPENAI_BASE_URL", "") or "").strip()
            ),
            "detail": "OPENAI_API_KEY / OPENAI_BASE_URL",
        },
    ]
# ---------------------------------------------------------------------------
# async probes
# ---------------------------------------------------------------------------
async def probe_mcp(url: str, headers: dict | None = None) -> dict:
    """Minimal MCP streamable-HTTP handshake: initialize + tools/list."""
    started = time.time()
    base_headers = {
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            post_headers = {**base_headers, "Content-Type": "application/json"}
            init_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "voice-assistant-console",
                        "version": "1.0",
                    },
                },
            }
            response = await client.post(url, json=init_body, headers=post_headers)
            if response.status_code >= 400:
                return summarize_probe(
                    started, False, error=f"HTTP {response.status_code} on initialize"
                )
            session_id = response.headers.get("mcp-session-id", "")
            message = parse_mcp_response(
                response.text, response.headers.get("content-type", "")
            )
            if message.get("error"):
                return summarize_probe(
                    started, False, error=f"initialize error: {message['error']}"
                )

            if session_id:
                post_headers["mcp-session-id"] = session_id
            await client.post(
                url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=post_headers,
            )
            tools_response = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=post_headers,
            )
            if tools_response.status_code >= 400:
                # server reachable and initialized; tools/list is optional
                return summarize_probe(started, True)
            tools_message = parse_mcp_response(
                tools_response.text, tools_response.headers.get("content-type", "")
            )
            return summarize_probe(
                started,
                True,
                tools=extract_tool_names(tools_message.get("result") or {}),
            )
    except Exception as exc:  # noqa: BLE001 - report any probe failure
        return summarize_probe(started, False, error=f"{type(exc).__name__}: {exc}")


async def probe_livekit(env: dict) -> dict:
    """LiveKit server API check: list rooms + participants (device presence)."""
    url = ws_to_http(str(env.get("LIVEKIT_URL", "") or "http://localhost:7880"))
    api_key = str(env.get("LIVEKIT_API_KEY", "") or "")
    api_secret = str(env.get("LIVEKIT_API_SECRET", "") or "")
    started = time.time()
    if not api_key or not api_secret:
        return {
            "ok": False,
            "latency_ms": 0,
            "error": "LIVEKIT_API_KEY / LIVEKIT_API_SECRET not configured",
            "rooms": [],
            "participants": [],
        }
    try:
        from livekit import api as lk_api

        client = lk_api.LiveKitAPI(url, api_key, api_secret)
        try:
            rooms = await client.room.list_rooms(lk_api.ListRoomsRequest())
            participants: list[dict] = []
            for room in rooms.rooms:
                listed = await client.room.list_participants(
                    lk_api.ListParticipantsRequest(room=room.name)
                )
                for participant in listed.participants:
                    participants.append(
                        {
                            "identity": participant.identity,
                            "name": participant.name,
                            "room": room.name,
                            "joined_at": float(participant.joined_at or 0),
                        }
                    )
            return {
                "ok": True,
                "latency_ms": int((time.time() - started) * 1000),
                "error": "",
                "rooms": [room.name for room in rooms.rooms],
                "participants": participants,
            }
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 - report any probe failure
        return {
            "ok": False,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "rooms": [],
            "participants": [],
        }

