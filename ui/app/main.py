"""Voice assistant web console: configuration & diagnostics UI (FastAPI).

Replaces the former nginx-served test page. Serves:

* the single-page console UI (static/, no build toolchain)
* a cookie-authenticated REST API (settings, MCP servers, devices,
  connectivity status, audit log, token minting)
* an internal bearer-token API for the agent (runtime config, audit events)

Run with:  python serve.py   (or uvicorn main:app --app-dir ui/app)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles

import audit_core
import probes
import settings_core
from auth_core import (
    OidcConfig,
    SESSION_COOKIE,
    internal_token,
    new_secret,
    resolve_local_auth_mode,
    sign_session,
    verify_login,
    verify_session,
)
from db import Database
from oidc import OidcClient, OidcError

logger = logging.getLogger("console")

CONSOLE_VERSION = "1.0"
IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ConsoleState:
    """Everything the route handlers need, built once at startup."""

    def __init__(self, env: dict) -> None:
        self.env = env
        self.started_at = time.time()
        data_dir = Path(env.get("UI_DATA_DIR") or "/data")
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            data_dir = Path("./data")
            data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.db = Database(str(data_dir / "console.db"))

        self.oidc = OidcClient(OidcConfig.from_env(env))
        self.local_mode = resolve_local_auth_mode(env, self.oidc.config)

        # signing key: env, else generated once and kept in the data volume
        self.secret_key = str(env.get("UI_SECRET_KEY", "") or "").strip()
        if not self.secret_key:
            self.secret_key = self.db.get_meta("ui_secret_key") or new_secret()
            self.db.set_meta("ui_secret_key", self.secret_key)

        # first-run convenience: a generated password (logged once) so the
        # console is never locked out when UI_PASSWORD was forgotten
        auth_env = dict(env)
        if self.local_mode == "enabled" and not str(env.get("UI_PASSWORD", "") or "").strip():
            generated = self.db.get_meta("generated_password") or new_secret()
            self.db.set_meta("generated_password", generated)
            auth_env["UI_PASSWORD"] = generated
            auth_env.setdefault("UI_EMAIL", "admin@local")
            logger.warning(
                "UI_PASSWORD is empty - generated first-run console login: %s / %s "
                "(set UI_EMAIL / UI_PASSWORD in .env to replace it)",
                auth_env["UI_EMAIL"],
                generated,
            )
        if not auth_env.get("UI_EMAIL"):
            auth_env["UI_EMAIL"] = "admin@local"
        self.auth_env = auth_env

        self.internal = internal_token(env)
        self._mcp_probe_cache: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------
    def effective_settings(self) -> dict[str, str]:
        return settings_core.effective(self.env, self.db.get_settings())

    def stored_mcp_list(self) -> list[dict]:
        raw = self.db.get_settings().get(settings_core.MCP_SERVERS_KEY, "")
        if not raw:
            return []
        try:
            return settings_core.normalize_ui_mcp_list(json.loads(raw))
        except (ValueError, TypeError):
            logger.warning("stored mcp_servers setting is invalid; ignoring")
            return []

    def transcripts_enabled(self) -> bool:
        return bool(settings_core.parse_bool(
            self.effective_settings().get("transcripts_enabled"), False
        ))

    def record_event(
        self, event_type: str, data: dict | None = None,
        *, room: str = "", identity: str = "",
    ) -> None:
        """Console-originated audit event (config changes, logins, ...)."""
        self.db.insert_events(
            [
                {
                    "ts": time.time(),
                    "type": event_type,
                    "room": room,
                    "identity": identity,
                    "data": json.dumps(data or {}, default=str),
                }
            ]
        )

    def ingest_agent_events(self, raw_events: list) -> int:
        """Normalize + store agent events; maintain the device registry."""
        now = time.time()
        normalized = []
        for raw in raw_events or []:
            event = audit_core.normalize_event(
                raw, transcripts_enabled=self.transcripts_enabled(), now=now
            )
            if event is not None:
                normalized.append(event)
        self.db.insert_events(normalized)
        for event in normalized:
            if event["identity"]:
                self.db.upsert_device(
                    event["identity"], room=event["room"], seen_ts=event["ts"]
                )
            if event["type"] == "session.started":
                payload = event["data"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload or "{}")
                    except ValueError:
                        payload = {}
                for participant in payload.get("participants", []):
                    identity = str(participant.get("identity") or "")
                    if identity and not identity.lower().startswith("agent"):
                        self.db.upsert_device(
                            identity, room=event["room"], seen_ts=event["ts"],
                            count_session=True,
                        )
        return len(normalized)
# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(title="Voice Assistant Console", version=CONSOLE_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    state = ConsoleState(dict(os.environ))
    app.state.console = state

    # ------------------------------------------------------------------
    # auth helpers
    # ------------------------------------------------------------------
    def request_is_secure(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        return request.headers.get("x-forwarded-proto", "").lower() == "https"

    def current_user(request: Request) -> dict | None:
        token = request.cookies.get(SESSION_COOKIE, "")
        return verify_session(token, state.secret_key)

    async def require_user(request: Request) -> dict:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="authentication required")
        if request.url.path.startswith("/api/") and request.method in (
            "POST", "PUT", "PATCH", "DELETE",
        ):
            # CSRF defence: SameSite=Lax cookie + custom header on mutations
            if request.headers.get("x-va-request") != "1":
                raise HTTPException(status_code=403, detail="missing CSRF header")
        return user

    def require_internal(request: Request) -> None:
        if not state.internal:
            raise HTTPException(
                status_code=503,
                detail="internal API disabled: no LIVEKIT_API_SECRET / "
                       "CONSOLE_INTERNAL_TOKEN configured",
            )
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        import hmac as _hmac

        if not token or not _hmac.compare_digest(token, state.internal):
            raise HTTPException(status_code=401, detail="invalid internal token")

    def session_token(user: str, method: str) -> str:
        return sign_session({"sub": user, "method": method}, state.secret_key)

    # ------------------------------------------------------------------
    # auth endpoints
    # ------------------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": CONSOLE_VERSION}

    @app.get("/auth/methods")
    async def auth_methods(request: Request) -> dict:
        return {
            "local": state.local_mode,  # enabled | disabled | setup
            "local_email": state.auth_env.get("UI_EMAIL", ""),
            "oidc": state.oidc.config.enabled,
            "authenticated": current_user(request) is not None,
        }

    @app.post("/auth/login")
    async def auth_login(request: Request) -> JSONResponse:
        body = await request.json()
        email = str(body.get("email", "") or "")
        password = str(body.get("password", "") or "")
        if state.local_mode != "enabled":
            state.record_event("auth.failed", {"reason": "local login disabled"})
            raise HTTPException(status_code=400, detail="local login is disabled")
        if not verify_login(email, password, state.auth_env):
            state.record_event(
                "auth.failed", {"email": email[:128], "reason": "bad credentials"}
            )
            raise HTTPException(status_code=401, detail="invalid email or password")
        email = str(state.auth_env.get("UI_EMAIL", email))
        state.record_event("auth.login", {"method": "local", "user": email})
        response = JSONResponse({"ok": True, "user": email, "method": "local"})
        response.set_cookie(
            SESSION_COOKIE,
            session_token(email, "local"),
            httponly=True,
            samesite="lax",
            secure=request_is_secure(request),
            max_age=12 * 3600,
            path="/",
        )
        return response
    # ------------------------------------------------------------------
    # OIDC (SSO) endpoints
    # ------------------------------------------------------------------
    @app.get("/auth/oidc/login")
    async def oidc_login(request: Request):
        if not state.oidc.config.enabled:
            raise HTTPException(status_code=404, detail="OIDC not configured")
        metadata = await state.oidc.metadata()
        state_value = state.oidc.new_state()
        redirect_uri = state.oidc.redirect_uri_for(request)
        redirect = RedirectResponse(
            state.oidc.authorization_url(metadata, redirect_uri, state_value),
            status_code=303,
        )
        # short-lived signed state cookie round-trips through the provider
        redirect.set_cookie(
            "va_oidc_state",
            sign_session({"state": state_value, "redirect_uri": redirect_uri},
                         state.secret_key, ttl=600),
            httponly=True, samesite="lax",
            secure=request_is_secure(request), max_age=600, path="/",
        )
        return redirect

    @app.get("/auth/oidc/callback")
    async def oidc_callback(request: Request):
        if not state.oidc.config.enabled:
            raise HTTPException(status_code=404, detail="OIDC not configured")
        params = request.query_params
        state_cookie = request.cookies.get("va_oidc_state", "")
        expected = verify_session(state_cookie, state.secret_key) or {}
        if not expected or params.get("state") != expected.get("state"):
            return _oidc_error_page("Login failed: state mismatch (please retry).")
        if params.get("error"):
            return _oidc_error_page(f"Provider error: {params.get('error')}")
        try:
            email = await state.oidc.complete_login(
                request, str(params.get("code", ""))
            )
        except OidcError as exc:
            state.record_event("auth.failed", {"method": "oidc", "reason": str(exc)})
            return _oidc_error_page(f"Login failed: {exc}")
        state.record_event("auth.login", {"method": "oidc", "user": email})
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("va_oidc_state", path="/")
        response.set_cookie(
            SESSION_COOKIE,
            session_token(email, "oidc"),
            httponly=True, samesite="lax",
            secure=request_is_secure(request), max_age=12 * 3600, path="/",
        )
        return response

    def _oidc_error_page(message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<html><body style='font-family:system-ui;background:#10151c;"
            f"color:#e6edf3'><h2>Sign-in</h2><p>{message}</p>"
            f"<p><a style='color:#7ab8ff' href='/'>Back to the console</a></p>"
            f"</body></html>",
            status_code=401,
        )

    # ------------------------------------------------------------------
    # session / settings API
    # ------------------------------------------------------------------
    @app.get("/api/session")
    async def api_session(user: dict = Depends(require_user)) -> dict:
        return {"user": user.get("sub", ""), "method": user.get("method", "")}

    @app.post("/auth/logout")
    async def auth_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/settings")
    async def api_settings_get(user: dict = Depends(require_user)) -> dict:
        return {
            "settings": settings_core.redact_for_client(
                state.env, state.db.get_settings()
            ),
            "environment": settings_core.env_only_status(state.env),
            "mcp_servers": settings_core.merged_mcp_view(
                state.stored_mcp_list(),
                settings_core.env_mcp_servers(state.env),
                state.env,
            ),
            "config_version": state.db.config_version(),
        }

    @app.put("/api/settings")
    async def api_settings_put(
        request: Request, user: dict = Depends(require_user)
    ) -> dict:
        body = await request.json()
        updates, skipped = settings_core.sanitize_updates(body.get("updates") or {})
        problems = settings_core.validate_updates(updates, state.env)
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))
        if updates:
            state.db.set_settings(updates, updated_by=user.get("sub", ""))
            state.record_event(
                "config.changed",
                {
                    "by": user.get("sub", ""),
                    "keys": sorted(updates.keys()),
                    "values": {
                        k: ("***" if settings_core._def(k).kind == settings_core.KIND_SECRET else v)
                        for k, v in updates.items()
                    },
                },
            )
            state.db.bump_config_version()
        return {
            "ok": True,
            "skipped": skipped,
            "problems": [],
            "config_version": state.db.config_version(),
            "note": "Applies to newly started assistant sessions",
        }
    # ------------------------------------------------------------------
    # MCP servers API (UI-managed list)
    # ------------------------------------------------------------------
    @app.get("/api/mcp-servers")
    async def api_mcp_get(user: dict = Depends(require_user)) -> dict:
        return {
            "servers": settings_core.merged_mcp_view(
                state.stored_mcp_list(),
                settings_core.env_mcp_servers(state.env),
                state.env,
            )
        }

    @app.put("/api/mcp-servers")
    async def api_mcp_put(
        request: Request, user: dict = Depends(require_user)
    ) -> dict:
        body = await request.json()
        try:
            server_list = settings_core.normalize_ui_mcp_list(body.get("servers"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        state.db.set_settings(
            {settings_core.MCP_SERVERS_KEY: json.dumps(server_list)},
            updated_by=user.get("sub", ""),
        )
        state.record_event(
            "config.changed",
            {
                "by": user.get("sub", ""),
                "keys": ["mcp_servers"],
                "values": {"count": len(server_list)},
            },
        )
        state.db.bump_config_version()
        return {
            "ok": True,
            "count": len(server_list),
            "note": "New MCP servers are picked up by the next assistant session",
        }

    @app.post("/api/mcp-servers/test")
    async def api_mcp_test(
        request: Request, user: dict = Depends(require_user)
    ) -> dict:
        """Probe one server: body = {"url": ..., "headers": {...}}."""
        body = await request.json()
        url = str(body.get("url", "") or "").strip()
        if not url:
            raise HTTPException(status_code=422, detail="url required")
        return await probes.probe_mcp(url, body.get("headers") or {})

    # ------------------------------------------------------------------
    # devices API
    # ------------------------------------------------------------------
    @app.get("/api/devices")
    async def api_devices(user: dict = Depends(require_user)) -> dict:
        livekit = await probes.probe_livekit(state.env)
        live = {
            p["identity"]: p for p in livekit.get("participants", []) if p["identity"]
        }
        devices = []
        for row in state.db.list_devices():
            identity = row["identity"]
            online = identity in live
            devices.append(
                {
                    "identity": identity,
                    "name": row["name"] or identity,
                    "kind": row["kind"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "last_room": row["last_room"],
                    "session_count": row["session_count"],
                    "online": online,
                    "current_room": live.get(identity, {}).get("room", ""),
                }
            )
        for identity, participant in live.items():
            if identity.lower().startswith("agent"):
                continue
            if identity not in {d["identity"] for d in devices}:
                devices.append(
                    {
                        "identity": identity,
                        "name": participant.get("name") or identity,
                        "kind": "browser" if identity.startswith("web-") else "device",
                        "first_seen": participant.get("joined_at", 0),
                        "last_seen": time.time(),
                        "last_room": participant.get("room", ""),
                        "session_count": 0,
                        "online": True,
                        "current_room": participant.get("room", ""),
                    }
                )
        return {
            "devices": devices,
            "livekit_ok": livekit["ok"],
            "livekit_error": livekit.get("error", ""),
        }

    @app.patch("/api/devices/{identity}")
    async def api_device_rename(
        identity: str, request: Request, user: dict = Depends(require_user)
    ) -> dict:
        body = await request.json()
        name = str(body.get("name", "") or "").strip()[:128]
        if not state.db.rename_device(identity, name):
            # unknown identity: register it with the given name
            if not IDENTITY_RE.match(identity):
                raise HTTPException(status_code=422, detail="invalid identity")
            state.db.upsert_device(identity)
            state.db.rename_device(identity, name)
        return {"ok": True}

    @app.delete("/api/devices/{identity}")
    async def api_device_delete(
        identity: str, user: dict = Depends(require_user)
    ) -> dict:
        state.db.delete_device(identity)
        return {"ok": True}
    # ------------------------------------------------------------------
    # connectivity status
    # ------------------------------------------------------------------
    async def collect_status() -> dict:
        now = time.time()
        livekit = await probes.probe_livekit(state.env)
        agent_age = state.db.last_event_age(
            ("agent.heartbeat", "agent.ready", "session.started"), now
        )
        servers = []
        for server in settings_core.merged_mcp_view(
            state.stored_mcp_list(),
            settings_core.env_mcp_servers(state.env),
            state.env,
        ):
            if not server["active"]:
                continue
            cached = state._mcp_probe_cache.get(server["id"])
            if cached and now - cached[0] < 60:
                result = cached[1]
            else:
                result = await probes.probe_mcp(server["url"])
                state._mcp_probe_cache[server["id"]] = (now, result)
            servers.append(
                {
                    "id": server["id"],
                    "url": server["url"],
                    "source": server["source"],
                    **result,
                }
            )
        return {
            "time": now,
            "console": {
                "ok": True,
                "version": CONSOLE_VERSION,
                "uptime_s": int(now - state.started_at),
            },
            "livekit": {
                "ok": livekit["ok"],
                "latency_ms": livekit.get("latency_ms", 0),
                "error": livekit.get("error", ""),
                "rooms": livekit.get("rooms", []),
            },
            "agent": {
                "ok": probes.heartbeat_online(agent_age),
                "last_seen_age_s": agent_age,
            },
            "providers": probes.provider_statuses(state.env),
            "mcp_servers": servers,
            "database": {
                "ok": True,
                "events": state.db.count_events(),
                "devices": len(state.db.list_devices()),
                "retention_days": int(
                    state.effective_settings().get("diagnostics_history_days")
                    or audit_core.DEFAULT_RETENTION_DAYS
                ),
            },
        }

    @app.get("/api/status")
    async def api_status(user: dict = Depends(require_user)) -> dict:
        return await collect_status()

    # ------------------------------------------------------------------
    # audit log API
    # ------------------------------------------------------------------
    @app.get("/api/events")
    async def api_events(
        request: Request, user: dict = Depends(require_user)
    ) -> dict:
        params = request.query_params
        rows = state.db.query_events(
            event_type=params.get("type", ""),
            identity=params.get("identity", ""),
            search=params.get("search", ""),
            limit=int(params.get("limit", "200")),
            before_id=int(params["before"]) if params.get("before") else None,
        )
        return {
            "events": [audit_core.parse_stored_event(row) for row in rows],
            "transcripts_enabled": state.transcripts_enabled(),
        }

    @app.get("/api/events/export")
    async def api_events_export(
        request: Request, user: dict = Depends(require_user)
    ):
        params = request.query_params
        rows = state.db.query_events(
            event_type=params.get("type", ""),
            identity=params.get("identity", ""),
            limit=1000,
        )
        events = [audit_core.parse_stored_event(row) for row in rows]
        if params.get("format") == "json":
            return JSONResponse({"events": events})
        filename = time.strftime("voice-assistant-audit-%Y%m%d-%H%M%S.csv")
        return PlainTextResponse(
            audit_core.events_to_csv(events),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.delete("/api/events")
    async def api_events_clear(user: dict = Depends(require_user)) -> dict:
        removed = state.db.clear_events()
        state.record_event(
            "config.changed",
            {"keys": ["events_cleared"], "values": {"removed": removed}},
        )
        return {"ok": True, "removed": removed}
    # ------------------------------------------------------------------
    # access token minting (Talk tab / adding devices)
    # ------------------------------------------------------------------
    @app.post("/api/tokens/mint")
    async def api_tokens_mint(
        request: Request, user: dict = Depends(require_user)
    ) -> dict:
        body = await request.json()
        identity = str(body.get("identity", "") or "").strip()
        if not IDENTITY_RE.match(identity):
            raise HTTPException(
                status_code=422,
                detail="identity must be 1-64 chars: letters, digits, . _ -",
            )
        values = state.effective_settings()
        room = (
            str(body.get("room", "") or "").strip()
            or values.get("room_name", "home")
        )
        try:
            hours = int(body.get("hours") or values.get("token_valid_hours", "12"))
        except ValueError:
            hours = 12
        api_key = str(state.env.get("LIVEKIT_API_KEY", "") or "")
        api_secret = str(state.env.get("LIVEKIT_API_SECRET", "") or "")
        if not api_key or not api_secret:
            raise HTTPException(
                status_code=503,
                detail="LIVEKIT_API_KEY / LIVEKIT_API_SECRET not configured",
            )
        from datetime import timedelta

        from livekit import api as lk_api

        token = (
            lk_api.AccessToken(api_key, api_secret)
            .with_identity(identity)
            .with_name(identity)
            .with_grants(
                lk_api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .with_ttl(timedelta(hours=hours))
            .to_jwt()
        )
        url = str(
            state.env.get("PUBLIC_LIVEKIT_WS_URL", "")
            or state.env.get("LIVEKIT_URL", "")
            or ""
        )
        state.record_event(
            "token.minted", {"identity": identity, "room": room, "hours": hours}
        )
        return {
            "token": token,
            "url": url,
            "room": room,
            "identity": identity,
            "hours": hours,
        }

    # ------------------------------------------------------------------
    # internal API for the agent (bearer token)
    # ------------------------------------------------------------------
    @app.get("/internal/config")
    async def internal_config(request: Request):
        require_internal(request)
        return settings_core.agent_runtime_payload(
            state.env,
            state.db.get_settings(),
            state.stored_mcp_list(),
            state.db.config_version(),
        )

    @app.post("/internal/events")
    async def internal_events(request: Request):
        require_internal(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        accepted = state.ingest_agent_events(body.get("events") or [])
        return {"ok": True, "accepted": accepted}

    @app.get("/internal/healthz")
    async def internal_healthz(request: Request):
        require_internal(request)
        return {"ok": True}

    # ------------------------------------------------------------------
    # static UI
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/talk", include_in_schema=False)
    async def talk_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "talk" / "index.html")

    app.mount(
        "/static", StaticFiles(directory=str(STATIC_DIR), html=True),
        name="static",
    )

    # ------------------------------------------------------------------
    # retention background task
    # ------------------------------------------------------------------
    async def retention_loop() -> None:
        while True:
            try:
                days = int(
                    state.effective_settings().get("diagnostics_history_days")
                    or audit_core.DEFAULT_RETENTION_DAYS
                )
                removed = state.db.clear_events(
                    before_ts=audit_core.retention_cutoff(days, time.time())
                )
                if removed:
                    logger.info("retention: removed %d expired events", removed)
            except Exception:  # noqa: BLE001 - never kill the loop
                logger.exception("retention task failed")
            await asyncio.sleep(3600)

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.retention_task = asyncio.create_task(retention_loop())
        logger.info(
            "console ready on port %s (local auth: %s, oidc: %s)",
            state.env.get("UI_PORT", "8090"),
            state.local_mode,
            state.oidc.config.enabled,
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "retention_task", None)
        if task:
            task.cancel()

    return app


app = create_app()





