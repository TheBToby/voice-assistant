# Web console (configuration & diagnostics)

The console is the assistant's built-in admin UI, served by the `console`
container (default `http://<host>:8090`, `UI_PORT` in `.env`). It replaces
the former static browser test page; that page lives on as the console's
**Talk** tab (`/talk`).

What it gives you:

| Tab | Purpose |
|---|---|
| **Dashboard** | Connectivity at a glance: LiveKit server, agent worker heartbeat, MCP server probes (with discovered tools), provider key status, audit log size |
| **Devices** | Every device identity ever seen (reSpeaker, browsers): friendly names, online state (from the LiveKit server), rooms, last seen, session counts; mint access tokens |
| **MCP Servers** | Add / edit / enable / remove MCP servers at runtime - no `.env` edit, no restart; a **Test** button probes each server and lists its tools |
| **Settings** | All non-fundamental settings (persona, models, Home Assistant, diagnostics retention) with env defaults + stored overrides |
| **Audit Log** | Interaction trail (sessions, tools, timers, logins, config changes), filterable, exportable (CSV/JSON), retention configurable in days |
| **Talk** | The browser test client: one click mints a token server-side and joins the default room |

## Access control

Two login methods, both configured in `.env`:

1. **Local email + password** (default): `UI_EMAIL` / `UI_PASSWORD`.
   Comparisons are constant-time; sessions are HttpOnly signed cookies
   (12 h). If `UI_PASSWORD` is left empty, the console generates a one-off
   password on first start and prints it **to the console container log**
   (`docker compose logs console`) so you are never locked out - set
   `UI_EMAIL`/`UI_PASSWORD` properly afterwards.
2. **SSO via OIDC** (recommended): set `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`
   and `OIDC_CLIENT_SECRET` to enable the authorization-code flow with any
   standard provider (Authentik, Keycloak, Auth0, Dex, ...). Optional:
   `OIDC_SCOPES` (default `openid profile email`), `OIDC_REDIRECT_URL`
   (only needed behind the TLS path proxy), and allow-lists via
   `OIDC_ALLOWED_EMAILS` / `OIDC_ALLOWED_DOMAINS` (comma separated, empty =
   everyone authenticated at the provider gets in).

With OIDC configured, the password form hides automatically
(`UI_LOCAL_AUTH=false` forces SSO-only; `true` always shows both).

Security notes: mutating API calls require the `X-VA-Request: 1` header
(CSRF defence on top of `SameSite=Lax` cookies); cookies are `Secure`
automatically behind HTTPS; the internal agent API is protected by a bearer
token (see below).

## Settings: env defaults + UI overrides

The console distinguishes:

- **Fundamental, one-time settings** (stay in `.env`, shown read-only in the
  UI): `LIVEKIT_API_KEY/SECRET`, provider API keys, LiveKit URLs, ports, TZ,
  data dirs, and everything about the console itself (auth, OIDC).
- **Everything else** is editable in the UI: assistant name, language,
  system-prompt override, greeting, turn detector, default location, weather
  units, STT/TTS model and voice, LLM model, LLM base URL (local LLMs),
  Home Assistant URL/token, the default room and token lifetime, diagnostics
  retention and transcript storage.

Env values are the defaults; values saved in the UI are stored in the
console database (`console-data` volume) and shown as *(override saved)*.
Clearing a field removes the override again. **Changes apply to newly
started assistant sessions** - the agent pulls the effective configuration
at the start of every session, so a running conversation finishes with the
old settings and no container restart is ever needed.

> Home Assistant credentials live in the console database once saved from the
> UI - treat the `console-data` volume as sensitive (it is under your Docker
> root, same as `.env` would be).

## MCP servers without `.env`

The **MCP Servers** tab manages the same registry that `MCP_SERVERS_JSON`
used to own, plus Home Assistant:

- UI-managed entries can be added, edited, enabled/disabled and removed at
  runtime. **Test** performs a real MCP handshake (`initialize` +
  `tools/list`) and shows the discovered tools and latency.
- Entries from `MCP_SERVERS_JSON` (env) and the Home Assistant integration
  are listed read-only. A UI entry with the same id **overrides** them
  (first definition wins - the same rule the agent applies).

## Diagnostics & audit trail

- **Devices** are learned automatically: when a participant joins a room the
  agent reports it; the console keeps a registry (identity, kind, friendly
  name, first/last seen, session count). Online state comes live from the
  LiveKit server API. Known identities can be renamed or removed.
- **Connectivity**: dashboard probes LiveKit (server API), the agent worker
  (heartbeat events, max. 120 s age), each active MCP server (60 s result
  cache) and checks provider key configuration.
- **Audit trail** events: `session.started/ended`, `device.join/leave`,
  `user_input`, `agent_reply`, `tool.call` (built-in + MCP tools with
  arguments and errors), `timer.expired`, `agent.ready`, `agent.heartbeat`,
  `error`, plus console events `config.changed` (who changed what),
  `token.minted`, `auth.login`, `auth.failed`.

**Privacy**: transcript storage is **off by default** - the audit trail then
contains interaction *metadata* only (that a session happened, which tools
ran, that something failed). User utterances and assistant replies are only
stored after enabling *Settings → Diagnostics → Store transcripts*. The
setting is enforced on both sides (agent and console) and applies to new
events immediately.

**Retention**: *Diagnostics history (days)* (default 30, env default
`DIAGNOSTICS_HISTORY_DAYS`) - an hourly cleanup task deletes older events.
Export everything as CSV/JSON from the audit tab, or clear it manually.

## Agent <-> console internals

The agent talks to the console over two bearer-token endpoints:

- `GET /internal/config` - effective settings + console-managed MCP servers,
  fetched at every session start (falls back to env-only when the console is
  unreachable).
- `POST /internal/events` - audit event batches (queued in the agent,
  flushed every 2 s, dropped silently if the console is down - diagnostics
  can never break the voice pipeline).

The shared token: `CONSOLE_INTERNAL_TOKEN` if set, otherwise derived from
`LIVEKIT_API_SECRET` (which both containers already know), so it works with
zero extra configuration. Disable the whole thing with
`CONSOLE_AUDIT_DISABLED=true` in the agent environment.

## Ports & TLS

| Port | Service |
|---|---|
| `UI_PORT` (default 8090) | console UI + API (host networking) |

With the `tls` profile the console is additionally reachable at
`https://<VA_DOMAIN>/console` (set `UI_ROOT_PATH=/console` in `.env`, and
`OIDC_REDIRECT_URL=https://<VA_DOMAIN>/console/auth/oidc/callback` when SSO
is enabled). Devices keep connecting to LiveKit directly as before.

