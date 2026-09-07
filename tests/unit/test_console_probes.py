"""Tests for the console's MCP diagnostics (ui/app/probes.py).

The pure parsing helpers are tested directly. The end-to-end probe tests
speak to minimal MCP servers built on the stdlib HTTP server, covering the
transports the agent's MCPServerHTTP accepts (streamable HTTP with JSON and
SSE responses, plus the legacy HTTP+SSE transport) and the auth-header
regression that made healthy servers show up as failed on the dashboard.
"""

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui" / "app"))

import probes  # noqa: E402
import settings_core  # noqa: E402

# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_parse_mcp_response_json():
    message = probes.parse_mcp_response(
        '{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}', "application/json"
    )
    assert message["id"] == 1


def test_sse_data_frames_multiline():
    frames = probes.sse_data_frames(
        'event: message\ndata: {"a":\ndata: 1}\n\nevent: ping\ndata: keep\n'
    )
    assert frames == ['{"a":\n1}', "keep"]


def test_parse_mcp_response_sse_selects_response_by_id():
    # a notification arrives before the response; the response body itself is
    # split over two data lines - the parser must pick the id:1 response
    body = (
        'event: message\ndata: {"jsonrpc":"2.0","method":"notify"}\n\n'
        'data: {"jsonrpc":"2.0",\ndata: "id":1,"result":{"ok":true}}\n\n'
    )
    message = probes.parse_mcp_response(body, "text/event-stream", expect_id=1)
    assert message["id"] == 1
    assert message["result"] == {"ok": True}


def test_parse_mcp_response_rejects_html_and_empty():
    with pytest.raises(ValueError, match="HTML"):
        probes.parse_mcp_response("<html>login</html>", "text/html")
    with pytest.raises(ValueError, match="empty"):
        probes.parse_mcp_response("", "application/json")
    with pytest.raises(ValueError, match="JSON-RPC frame"):
        probes.parse_mcp_response("data: not-json\n\n", "text/event-stream")


def test_extract_tool_names_and_server_info():
    assert probes.extract_tool_names(
        {"tools": [{"name": "a"}, {"name": "b"}, "junk", {"nope": 1}]}
    ) == ["a", "b"]
    assert probes.extract_tool_names({"nothing": 1}) == []
    assert probes.extract_server_info(
        {"serverInfo": {"name": "ha-mcp", "version": "2025.1"}}
    ) == {"name": "ha-mcp", "version": "2025.1"}
    assert probes.extract_server_info({}) == {}


def test_summarize_probe_shape():
    result = probes.summarize_probe(time.time(), True, tools=["x"])
    assert result["ok"] is True and result["tools"] == ["x"]
    assert result["error"] == ""
    assert result["server_info"] == {}
    assert result["protocol_version"] == ""
    assert isinstance(result["latency_ms"], int)


def test_status_hint_auth():
    hint = probes.status_hint(401)
    assert "401" in hint and "Authorization" in hint
    assert probes.status_hint(503).startswith("HTTP 503")


def test_merged_view_raw_headers_for_probing():
    env = {
        "HOME_ASSISTANT_URL": "http://ha:8123/",
        "HOME_ASSISTANT_TOKEN": "ha-token",
    }
    ui_list = settings_core.normalize_ui_mcp_list(
        [{"id": "w", "url": "http://w/mcp",
          "headers": {"Authorization": "Bearer xyz"}}]
    )
    # display: masked (existing behaviour)
    display = settings_core.merged_mcp_view(ui_list, [], env)
    by_id = {s["id"]: s for s in display}
    assert by_id["w"]["headers"]["Authorization"].endswith("***")
    ha_masked = by_id["home-assistant"]["headers"]["Authorization"]
    assert ha_masked.startswith("Bearer ") and ha_masked.endswith("***")
    assert "ha-token" not in ha_masked
    # probing: real headers, but they must never reach the client payload
    raw = settings_core.merged_mcp_view(ui_list, [], env, mask_secrets=False)
    by_id = {s["id"]: s for s in raw}
    assert by_id["w"]["headers"]["Authorization"] == "Bearer xyz"
    assert by_id["home-assistant"]["headers"]["Authorization"] == "Bearer ha-token"


# ---------------------------------------------------------------------------
# minimal stdlib MCP servers for the end-to-end probe tests
# ---------------------------------------------------------------------------
class _QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def _json_bytes(payload) -> bytes:
    return b"" if payload is None else json.dumps(payload).encode()


def run_mcp_server(handler) -> _QuietHTTPServer:
    server = _QuietHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def server_url(server: _QuietHTTPServer, path: str = "/mcp") -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


def probe(url: str, headers: dict | None = None) -> dict:
    return asyncio.run(probes.probe_mcp(url, headers))


class StreamableJSONHandler(BaseHTTPRequestHandler):
    """Streamable-HTTP MCP server: Bearer auth, JSON responses."""

    token = "secret-token"
    tools_error = False  # subclass hook: answer tools/list with an RPC error

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._json(401, {"error": "unauthorized"})
        method = body.get("method")
        if method == "initialize":
            return self._json(200, {
                "jsonrpc": "2.0", "id": 1,
                "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "serverInfo": {"name": "test-server", "version": "2.5"},
                },
            })
        if method == "tools/list" and self.tools_error:
            return self._json(200, {
                "jsonrpc": "2.0", "id": 2,
                "error": {"code": -32601, "message": "Method not found"},
            })
        if method == "tools/list":
            return self._json(200, {
                "jsonrpc": "2.0", "id": 2,
                "result": {"tools": [{"name": "lights_on"}, {"name": "weather"}]},
            })
        return self._json(202, None)  # notification

    def _json(self, status: int, payload) -> None:
        data = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


class StreamableSSEHandler(StreamableJSONHandler):
    """Same handshake, but answers with SSE bodies (multi-frame, multi-line)."""

    def _json(self, status: int, payload) -> None:
        if payload is None:
            data = b""
        elif payload.get("id") == 1:
            data = (
                'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n'
                'data: {"jsonrpc":"2.0",\ndata: "id":1,"result":'
                '{"protocolVersion":"2025-06-18","serverInfo":'
                '{"name":"sse-server","version":"1.1"}}}\n\n'
            ).encode()
        else:
            data = (
                'data: {"jsonrpc":"2.0","id":2,"result":{"tools":'
                '[{"name":"tool_a"},{"name":"tool_b"}]}}\n\n'
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


class LegacySSEHandler(BaseHTTPRequestHandler):
    """Legacy HTTP+SSE transport: GET /sse stream + POST /messages."""

    protocol_version = "HTTP/1.1"
    streams: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path != "/sse":
            return self._plain(404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.streams["probe"] = self.wfile  # registered before announcing it
        self.wfile.write(b"event: endpoint\ndata: /messages?session_id=abc\n\n")
        self.wfile.flush()
        try:
            while True:  # keep the event stream open
                time.sleep(0.05)
        except Exception:
            pass

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)  # always drain (keep-alive safety)
        if not self.path.startswith("/messages"):
            return self._plain(404)
        body = json.loads(raw or b"{}")
        wfile = self.streams.get("probe")
        response = None
        if body.get("method") == "initialize":
            response = {
                "jsonrpc": "2.0", "id": 1,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "legacy-sse", "version": "0.9"},
                },
            }
        elif body.get("id") == 2:
            response = {
                "jsonrpc": "2.0", "id": 2,
                "result": {"tools": [{"name": "legacy_tool"}]},
            }
        if response and wfile:
            wfile.write(
                f"event: message\ndata: {json.dumps(response)}\n\n".encode()
            )
            wfile.flush()
        self._plain(202)

    def _plain(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture(autouse=True)
def _reset_legacy_streams():
    LegacySSEHandler.streams = {}
    yield
    LegacySSEHandler.streams = {}


def require_real_httpx() -> None:
    """Skip unless a real httpx is importable.

    test_smoke_helpers stubs httpx (AsyncClient = object) when it is not
    installed - with that stub present, importorskip would still find a
    module, so the probe tests must detect the stub explicitly.
    """
    pytest.importorskip("httpx")
    import httpx

    if getattr(httpx, "AsyncClient", None) is object:
        pytest.skip("httpx is stubbed by test_smoke_helpers")


# ---------------------------------------------------------------------------
# end-to-end probe tests (need httpx, like the console image has)
# ---------------------------------------------------------------------------
def test_probe_streamable_json_sends_auth_headers():
    require_real_httpx()
    server = run_mcp_server(StreamableJSONHandler)
    try:
        url = server_url(server)
        # regression: without the configured headers a healthy server fails
        failed = probe(url)
        assert failed["ok"] is False
        assert "401" in failed["error"] and "Authorization" in failed["error"]
        # with the headers from the merged server view it works
        result = probe(url, {"Authorization": "Bearer secret-token"})
        assert result["ok"] is True, result["error"]
        assert result["tools"] == ["lights_on", "weather"]
        assert result["server_info"] == {"name": "test-server", "version": "2.5"}
        assert result["protocol_version"] == "2025-03-26"
        assert result["error"] == ""
    finally:
        server.shutdown()
        server.server_close()


def test_probe_streamable_sse_response():
    require_real_httpx()
    server = run_mcp_server(StreamableSSEHandler)
    try:
        result = probe(
            server_url(server), {"Authorization": "Bearer secret-token"}
        )
        assert result["ok"] is True, result["error"]
        assert result["tools"] == ["tool_a", "tool_b"]
        assert result["server_info"]["name"] == "sse-server"
        assert result["protocol_version"] == "2025-06-18"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_reports_tools_list_warning():
    require_real_httpx()
    server = run_mcp_server(StreamableJSONHandler)
    server.RequestHandlerClass.tools_error = True
    try:
        result = probe(
            server_url(server), {"Authorization": "Bearer secret-token"}
        )
        assert result["ok"] is True  # the server itself is healthy
        assert result["tools"] == []
        assert "tools/list error" in result["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_probe_falls_back_to_legacy_sse_transport():
    require_real_httpx()
    server = run_mcp_server(LegacySSEHandler)
    try:
        result = probe(server_url(server, "/sse"))
        assert result["ok"] is True, result["error"]
        assert result["tools"] == ["legacy_tool"]
        assert result["server_info"] == {"name": "legacy-sse", "version": "0.9"}
        assert "legacy SSE" in result["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_probe_reports_connection_failures():
    require_real_httpx()
    # grab a port and close it again: nothing listens there
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = probe(f"http://127.0.0.1:{port}/mcp")
    assert result["ok"] is False
    assert result["error"]  # human-readable reason
    assert result["tools"] == []
    bad = probe("ftp://nope")
    assert bad["ok"] is False and "unsupported URL" in bad["error"]