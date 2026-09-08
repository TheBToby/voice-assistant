"""Connectivity probes for the console diagnostics.

Network calls (httpx + LiveKit server API) with the *interpretation* of the
results kept in small pure helpers so the interesting logic is testable.

MCP probe: speaks the HTTP transports of the Model Context Protocol directly
with httpx, so no MCP SDK is needed in the console image:

* streamable HTTP (initialize -> notifications/initialized -> tools/list on
  one URL). JSON and SSE-style responses are both parsed, since MCPServerHTTP
  in the agent accepts both, and all offered protocol versions are tried.
* the legacy HTTP+SSE transport as a fallback (GET opens an event stream that
  announces the POST endpoint and also carries the JSON-RPC responses).

Authentication headers configured for a server are sent along. Without them,
servers such as the Home Assistant MCP integration correctly answer 401 - and
a healthy server would show up as failed on the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urljoin

PROBE_TIMEOUT = 5.0
# Offered newest-first during initialize; the server picks a version it
# supports and answers with it (strict servers error, which we also handle).
MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
AGENT_HEARTBEAT_MAX_AGE = 120.0  # seconds before the agent counts as offline


class NotStreamableEndpoint(Exception):
    """The URL refused MCP POSTs (404/405) - legacy SSE may still work."""


# ---------------------------------------------------------------------------
# pure result helpers
# ---------------------------------------------------------------------------
def heartbeat_online(age_seconds: float | None) -> bool:
    return age_seconds is not None and age_seconds <= AGENT_HEARTBEAT_MAX_AGE


def summarize_probe(
    started: float,
    ok: bool,
    tools: list[str] | None = None,
    error: str = "",
    server_info: dict | None = None,
    protocol_version: str = "",
    tool_details: list[dict] | None = None,
) -> dict:
    """Uniform probe result. `error` may carry a warning while ok=True."""
    return {
        "ok": ok,
        "latency_ms": int((time.time() - started) * 1000),
        "tools": tools or [],
        "tool_details": tool_details or [],
        "error": error,
        "server_info": server_info or {},
        "protocol_version": protocol_version,
    }


def status_hint(status_code: int) -> str:
    """HTTP status with an actionable hint for the dashboard."""
    hints = {
        400: " - the server rejected the request (URL path, Content-Type or "
        "protocol version)",
        401: " - unauthorized: check the Authorization header / access token",
        403: " - forbidden: the token is valid but the user lacks permission",
        404: " - not found: check the URL path (Home Assistant: "
        "http://<host>:8123/api/mcp)",
        405: " - method not allowed: the URL does not accept MCP POSTs",
        406: " - the server requires Accept: application/json, text/event-stream",
    }
    suffix = hints.get(status_code, "")
    return f"HTTP {status_code}{suffix}" if suffix else f"HTTP {status_code}"


def sse_data_frames(text: str) -> list[str]:
    """Data payloads of an SSE body; multi-line data joined per event."""
    frames: list[str] = []
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            lines.append(line[5:].strip())
        elif not line.strip():  # blank line ends the event
            if lines:
                frames.append("\n".join(lines))
            lines = []
    if lines:
        frames.append("\n".join(lines))
    return frames


def parse_mcp_response(
    text: str, content_type: str, expect_id: int | None = None
) -> dict:
    """Extract a JSON-RPC message from a JSON or SSE body. Raises ValueError.

    SSE bodies may contain several frames (e.g. notifications before the
    response); when `expect_id` is given the frame answering that JSON-RPC
    request id is preferred.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    if "text/html" in (content_type or "").lower():
        raise ValueError(
            "server returned HTML instead of a JSON-RPC response "
            "(wrong URL, or a login page in front of it?)"
        )
    if "text/event-stream" in (content_type or "").lower():
        candidates: list[dict] = []
        for frame in sse_data_frames(text):
            if frame == "[DONE]":
                continue
            try:
                message = json.loads(frame)
            except ValueError:
                continue
            if isinstance(message, dict):
                candidates.append(message)
        if not candidates:
            raise ValueError("no JSON-RPC frame found in SSE response")
        if expect_id is not None:
            for message in candidates:
                if message.get("id") == expect_id:
                    return message
        for message in candidates:
            if "result" in message or "error" in message:
                return message
        return candidates[0]
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"response is not valid JSON ({exc})") from exc


def extract_tool_names(result: dict) -> list[str]:
    """Tool names from a tools/list result, tolerating shape differences."""
    return [
        entry["name"] for entry in extract_tools(result)
    ]


def extract_tools(result: dict) -> list[dict]:
    """Tool details from a tools/list result, tolerating shape differences.

    Returns [{"name", "description"?, "input_schema"?}, ...] - whatever the
    server offers per tool, so the console can show a real tools overview
    instead of bare names.
    """
    tools = result.get("tools") if isinstance(result, dict) else None
    details: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        entry = {"name": str(tool["name"])}
        if tool.get("description"):
            entry["description"] = str(tool["description"])
        if isinstance(tool.get("inputSchema"), dict):
            entry["input_schema"] = tool["inputSchema"]
        details.append(entry)
    return details


def extract_server_info(result: dict) -> dict:
    """{name, version} from an initialize result (best effort)."""
    info = result.get("serverInfo") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        return {}
    out: dict = {}
    if info.get("name"):
        out["name"] = str(info["name"])
    if info.get("version"):
        out["version"] = str(info["version"])
    return out


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
def _initialize_body(protocol_version: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {
                "name": "voice-assistant-console",
                "version": "1.0",
            },
        },
    }


def _rpc_error(message: dict, action: str) -> str:
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        text = str(error.get("message") or error)
        return (
            f"{action} error {code}: {text}"
            if code is not None
            else f"{action} error: {text}"
        )
    return f"{action} error: {error}"


def _looks_like_version_mismatch(text: str) -> bool:
    lowered = text.lower()
    return "protocol" in lowered or "version" in lowered


def _network_error_text(exc: Exception) -> str:
    """Friendlier message for connection-level probe failures."""
    name = type(exc).__name__
    text = str(exc) or name
    if isinstance(exc, asyncio.TimeoutError) or name in {
        "TimeoutException", "ConnectTimeout", "ReadTimeout",
        "WriteTimeout", "PoolTimeout",
    }:
        return f"timeout after {PROBE_TIMEOUT:.0f}s: {text}"
    if name in {"ConnectError", "ConnectionError"}:
        return f"connection failed: {text}"
    return f"{name}: {text}"


async def _probe_streamable(
    client, url: str, base_headers: dict, started: float
) -> dict:
    """Probe the streamable-HTTP transport (JSON or SSE responses)."""
    post_headers = {**base_headers, "Content-Type": "application/json"}
    last_error = ""
    response = None
    message: dict = {}
    negotiated = ""

    for version in MCP_PROTOCOL_VERSIONS:
        response = await client.post(
            url,
            json=_initialize_body(version),
            headers=post_headers,
        )
        if response.status_code in (404, 405):
            raise NotStreamableEndpoint(
                f"HTTP {response.status_code} on initialize"
            )
        if response.status_code >= 400:
            last_error = f"initialize failed: {status_hint(response.status_code)}"
            if response.status_code == 400 and version != MCP_PROTOCOL_VERSIONS[-1]:
                continue  # may be an unsupported protocol version - try next
            return summarize_probe(started, False, error=last_error)
        message = parse_mcp_response(
            response.text, response.headers.get("content-type", ""), expect_id=1
        )
        if message.get("error"):
            last_error = _rpc_error(message, "initialize")
            if (
                _looks_like_version_mismatch(last_error)
                and version != MCP_PROTOCOL_VERSIONS[-1]
            ):
                continue  # strict server - retry with the next older version
            return summarize_probe(started, False, error=last_error)
        negotiated = str((message.get("result") or {}).get("protocolVersion") or version)
        break
    else:  # every offered version was rejected as a version mismatch
        return summarize_probe(started, False, error=last_error or "initialize failed")

    server_info = extract_server_info(message.get("result") or {})
    session_id = response.headers.get("mcp-session-id", "")
    session_headers = dict(post_headers)
    if session_id:
        session_headers["mcp-session-id"] = session_id
    if negotiated:
        session_headers["MCP-Protocol-Version"] = negotiated

    try:
        await client.post(
            url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        tools_response = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=session_headers,
        )
    except Exception as exc:  # noqa: BLE001 - server is healthy, tools are not
        return summarize_probe(
            started, True, server_info=server_info, protocol_version=negotiated,
            error=f"initialized ok, but the tools/list request failed: "
            f"{_network_error_text(exc)}",
        )

    if tools_response.status_code >= 400:
        # server reachable and initialized; tools/list is optional per spec
        return summarize_probe(
            started, True, server_info=server_info, protocol_version=negotiated,
            error=f"initialized, but tools/list failed: "
            f"{status_hint(tools_response.status_code)}",
        )
    tools_message = parse_mcp_response(
        tools_response.text,
        tools_response.headers.get("content-type", ""),
        expect_id=2,
    )
    if tools_message.get("error"):
        return summarize_probe(
            started, True, server_info=server_info, protocol_version=negotiated,
            error=f"initialized, but {_rpc_error(tools_message, 'tools/list')}",
        )
    tools_result = tools_message.get("result") or {}
    return summarize_probe(
        started, True,
        tools=extract_tool_names(tools_result),
        tool_details=extract_tools(tools_result),
        server_info=server_info, protocol_version=negotiated,
    )


async def _next_sse_event(frames: "asyncio.Queue") -> tuple[str, str] | None:
    """Next complete SSE event as (event, data); None on stream end."""
    event_name = ""
    data_lines: list[str] = []
    while True:
        line = await frames.get()
        if line is None:
            return None
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip():
            if data_lines:
                return event_name, "\n".join(data_lines)
            event_name = ""


async def _wait_for_rpc_response(
    frames: "asyncio.Queue", expected_id: int,
    timeout: float = PROBE_TIMEOUT,
) -> dict | None:
    """JSON-RPC response with `expected_id` from the SSE stream (or None)."""
    while True:
        try:
            event = await asyncio.wait_for(
                _next_sse_event(frames), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None
        if event is None:
            return None
        _, data = event
        if data == "[DONE]":
            continue
        try:
            message = json.loads(data)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == expected_id:
            return message


async def _probe_legacy_sse(
    client, url: str, base_headers: dict, started: float
) -> dict:
    """Probe the legacy HTTP+SSE transport (GET stream + POST endpoint).

    The GET announces the POST endpoint via an `endpoint` event and carries
    the JSON-RPC responses as `message` events, matched by request id.
    """
    frames: asyncio.Queue = asyncio.Queue()
    reader: asyncio.Task | None = None
    try:
        async with client.stream(
            "GET", url, headers={**base_headers, "Accept": "text/event-stream"}
        ) as stream:
            if stream.status_code >= 400:
                return summarize_probe(
                    started, False,
                    error=f"legacy SSE handshake failed: "
                    f"{status_hint(stream.status_code)}",
                )

            async def read_stream() -> None:
                try:
                    async for line in stream.aiter_lines():
                        await frames.put(line)
                except Exception:  # noqa: BLE001 - stream ends abruptly
                    pass
                finally:
                    await frames.put(None)

            reader = asyncio.create_task(read_stream())
            event = await _next_sse_event(frames)
            if event is None or event[0] != "endpoint":
                return summarize_probe(
                    started, False,
                    error="legacy SSE stream never announced an endpoint event",
                )
            post_url = urljoin(url, event[1])
            post_headers = {**base_headers, "Content-Type": "application/json"}
            init_response = await client.post(
                post_url,
                json=_initialize_body(MCP_PROTOCOL_VERSIONS[-1]),
                headers=post_headers,
            )
            if init_response.status_code >= 400:
                return summarize_probe(
                    started, False,
                    error=f"legacy SSE initialize failed: "
                    f"{status_hint(init_response.status_code)}",
                )
            init_message = await _wait_for_rpc_response(frames, 1)
            if init_message is None:
                return summarize_probe(
                    started, False,
                    error="no initialize response on the legacy SSE stream "
                    "(timeout)",
                )
            if init_message.get("error"):
                return summarize_probe(
                    started, False, error=_rpc_error(init_message, "initialize")
                )
            init_result = init_message.get("result") or {}
            server_info = extract_server_info(init_result)
            negotiated = str(init_result.get("protocolVersion") or "")

            await client.post(
                post_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=post_headers,
            )
            tools_response = await client.post(
                post_url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                      "params": {}},
                headers=post_headers,
            )
            if tools_response.status_code >= 400:
                return summarize_probe(
                    started, True, server_info=server_info,
                    protocol_version=negotiated,
                    error=f"initialized, but tools/list failed: "
                    f"{status_hint(tools_response.status_code)}",
                )
            tools_message = await _wait_for_rpc_response(frames, 2)
            if tools_message is None:
                return summarize_probe(
                    started, True, server_info=server_info,
                    protocol_version=negotiated,
                    error="initialized, but no tools/list response on the "
                    "legacy SSE stream (timeout)",
                )
            if tools_message.get("error"):
                return summarize_probe(
                    started, True, server_info=server_info,
                    protocol_version=negotiated,
                    error=f"initialized, but "
                    f"{_rpc_error(tools_message, 'tools/list')}",
                )
            legacy_tools = tools_message.get("result") or {}
            return summarize_probe(
                started, True,
                tools=extract_tool_names(legacy_tools),
                tool_details=extract_tools(legacy_tools),
                server_info=server_info, protocol_version=negotiated,
            )
    finally:
        if reader is not None:
            reader.cancel()


async def probe_mcp(url: str, headers: dict | None = None) -> dict:
    """Probe an MCP server: streamable HTTP first, legacy HTTP+SSE fallback.

    Performs the full handshake (initialize + tools/list) so the dashboard can
    report latency, negotiated protocol version, server name and tool names -
    not just reachability.
    """
    started = time.time()
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return summarize_probe(
            started, False,
            error=f"unsupported URL: {url or '(empty)'} - use http(s)://",
        )
    base_headers = {
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            try:
                return await _probe_streamable(
                    client, url, base_headers, started
                )
            except NotStreamableEndpoint as streamable_error:
                # fresh client/pool for the legacy transport: its long-lived
                # GET stream must not reuse a connection that carried POSTs
                async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as legacy_client:
                    legacy = await _probe_legacy_sse(
                        legacy_client, url, base_headers, started
                    )
                if legacy["ok"]:
                    legacy["error"] = (
                        legacy["error"]
                        or "connected via the legacy SSE transport"
                    )
                    return legacy
                return summarize_probe(
                    started, False,
                    error=f"streamable HTTP: {streamable_error}; "
                    f"legacy SSE: {legacy['error'] or 'no response'}",
                )
    except Exception as exc:  # noqa: BLE001 - report any probe failure
        return summarize_probe(started, False, error=_network_error_text(exc))


# ---------------------------------------------------------------------------
# tool calls (console "tool tester")
# ---------------------------------------------------------------------------
CALL_TIMEOUT = 30.0  # real tool runs may take notably longer than probes


def summarize_call(
    started: float,
    ok: bool,
    content: list[dict] | None = None,
    structured_content: dict | None = None,
    is_error: bool = False,
    error: str = "",
) -> dict:
    """Uniform tools/call result (ok=True means the RPC itself succeeded)."""
    return {
        "ok": ok,
        "latency_ms": int((time.time() - started) * 1000),
        "content": content or [],
        "structured_content": structured_content,
        "is_error": bool(is_error),
        "error": error,
    }


def _call_result_summary(started: float, message: dict, action: str) -> dict:
    """Shape a tools/call JSON-RPC response into a summarize_call result."""
    if message.get("error"):
        return summarize_call(started, False, error=_rpc_error(message, action))
    result = message.get("result")
    if not isinstance(result, dict):
        return summarize_call(
            started, False, error=f"{action}: unexpected response shape"
        )
    content: list[dict] = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            entry = {"type": str(item.get("type") or "unknown")}
            if item.get("text") is not None:
                entry["text"] = str(item["text"])
            if item.get("data") is not None:
                entry["data"] = str(item["data"])[:2000]
            content.append(entry)
    structured = result.get("structuredContent")
    is_error = bool(result.get("isError"))
    return summarize_call(
        started, True,
        content=content,
        structured_content=structured if isinstance(structured, dict) else None,
        is_error=is_error,
        error="the tool reported an error result" if is_error else "",
    )


def _tools_call_body(tool_name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


async def _call_tool_streamable(
    client, url: str, base_headers: dict, started: float,
    tool_name: str, arguments: dict,
) -> dict:
    """tools/call over the streamable-HTTP transport (JSON or SSE responses)."""
    post_headers = {**base_headers, "Content-Type": "application/json"}
    last_error = ""
    response = None
    negotiated = ""

    for version in MCP_PROTOCOL_VERSIONS:
        response = await client.post(
            url, json=_initialize_body(version), headers=post_headers
        )
        if response.status_code in (404, 405):
            raise NotStreamableEndpoint(
                f"HTTP {response.status_code} on initialize"
            )
        if response.status_code >= 400:
            return summarize_call(
                started, False,
                error=f"initialize failed: {status_hint(response.status_code)}",
            )
        message = parse_mcp_response(
            response.text, response.headers.get("content-type", ""), expect_id=1
        )
        if message.get("error"):
            last_error = _rpc_error(message, "initialize")
            if (
                _looks_like_version_mismatch(last_error)
                and version != MCP_PROTOCOL_VERSIONS[-1]
            ):
                continue  # strict server - retry with the next older version
            return summarize_call(started, False, error=last_error)
        negotiated = str((message.get("result") or {}).get("protocolVersion") or version)
        break
    else:  # every offered version was rejected as a version mismatch
        return summarize_call(started, False, error=last_error or "initialize failed")

    session_headers = dict(post_headers)
    session_id = response.headers.get("mcp-session-id", "")
    if session_id:
        session_headers["mcp-session-id"] = session_id
    if negotiated:
        session_headers["MCP-Protocol-Version"] = negotiated

    await client.post(
        url,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=session_headers,
    )
    call_response = await client.post(
        url,
        json=_tools_call_body(tool_name, arguments),
        headers=session_headers,
    )
    if call_response.status_code >= 400:
        return summarize_call(
            started, False,
            error=f"tools/call failed: {status_hint(call_response.status_code)}",
        )
    call_message = parse_mcp_response(
        call_response.text,
        call_response.headers.get("content-type", ""),
        expect_id=2,
    )
    return _call_result_summary(started, call_message, "tools/call")


async def _call_tool_legacy_sse(
    client, url: str, base_headers: dict, started: float,
    tool_name: str, arguments: dict,
) -> dict:
    """tools/call over the legacy HTTP+SSE transport."""
    frames: asyncio.Queue = asyncio.Queue()
    reader: asyncio.Task | None = None
    try:
        async with client.stream(
            "GET", url, headers={**base_headers, "Accept": "text/event-stream"}
        ) as stream:
            if stream.status_code >= 400:
                return summarize_call(
                    started, False,
                    error=f"legacy SSE handshake failed: "
                    f"{status_hint(stream.status_code)}",
                )

            async def read_stream() -> None:
                try:
                    async for line in stream.aiter_lines():
                        await frames.put(line)
                except Exception:  # noqa: BLE001 - stream ends abruptly
                    pass
                finally:
                    await frames.put(None)

            reader = asyncio.create_task(read_stream())
            event = await _next_sse_event(frames)
            if event is None or event[0] != "endpoint":
                return summarize_call(
                    started, False,
                    error="legacy SSE stream never announced an endpoint event",
                )
            post_url = urljoin(url, event[1])
            post_headers = {**base_headers, "Content-Type": "application/json"}
            init_response = await client.post(
                post_url,
                json=_initialize_body(MCP_PROTOCOL_VERSIONS[-1]),
                headers=post_headers,
            )
            if init_response.status_code >= 400:
                return summarize_call(
                    started, False,
                    error=f"legacy SSE initialize failed: "
                    f"{status_hint(init_response.status_code)}",
                )
            init_message = await _wait_for_rpc_response(frames, 1, CALL_TIMEOUT)
            if init_message is None:
                return summarize_call(
                    started, False,
                    error="no initialize response on the legacy SSE stream "
                    "(timeout)",
                )
            if init_message.get("error"):
                return summarize_call(
                    started, False, error=_rpc_error(init_message, "initialize")
                )
            await client.post(
                post_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=post_headers,
            )
            await client.post(
                post_url,
                json=_tools_call_body(tool_name, arguments),
                headers=post_headers,
            )
            call_message = await _wait_for_rpc_response(frames, 2, CALL_TIMEOUT)
            if call_message is None:
                return summarize_call(
                    started, False,
                    error="no tools/call response on the legacy SSE stream "
                    "(timeout)",
                )
            return _call_result_summary(started, call_message, "tools/call")
    finally:
        if reader is not None:
            reader.cancel()


async def call_tool(
    url: str,
    headers: dict | None = None,
    tool_name: str = "",
    arguments: dict | None = None,
) -> dict:
    """Call one tool on an MCP server (streamable HTTP, legacy SSE fallback).

    Speaks the same transports as probe_mcp(), but performs tools/call with
    user-supplied arguments - the console's tool tester. The result carries
    the raw tool content so the UI can show exactly what the server answered.
    """
    started = time.time()
    url = (url or "").strip()
    tool_name = (tool_name or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return summarize_call(
            started, False,
            error=f"unsupported URL: {url or '(empty)'} - use http(s)://",
        )
    if not tool_name:
        return summarize_call(started, False, error="tool name required")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return summarize_call(
            started, False, error="arguments must be a JSON object"
        )
    base_headers = {
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            try:
                return await _call_tool_streamable(
                    client, url, base_headers, started, tool_name, arguments
                )
            except NotStreamableEndpoint as streamable_error:
                # fresh client/pool for the legacy transport (see probe_mcp)
                async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as legacy_client:
                    legacy = await _call_tool_legacy_sse(
                        legacy_client, url, base_headers, started,
                        tool_name, arguments,
                    )
                if legacy["ok"]:
                    legacy["error"] = (
                        legacy["error"]
                        or "called via the legacy SSE transport"
                    )
                    return legacy
                return summarize_call(
                    started, False,
                    error=f"streamable HTTP: {streamable_error}; "
                    f"legacy SSE: {legacy['error'] or 'no response'}",
                )
    except Exception as exc:  # noqa: BLE001 - report any call failure
        return summarize_call(started, False, error=_network_error_text(exc))


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

