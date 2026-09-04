"""Authentication primitives for the web console.

Pure logic (no FastAPI imports) so it can be unit-tested on the host.

Two auth methods, both configured via environment variables:

* Local: fixed email + password (UI_EMAIL / UI_PASSWORD). Compared in
  constant time; the plaintext never leaves the server process.
* OIDC: authorization-code flow against any standard provider
  (see OidcConfig.from_env).

Browser sessions are stateless signed cookies (HMAC-SHA256 over a JSON
payload with an expiry), so console restarts keep users logged in and no
session table is needed. The signing key comes from UI_SECRET_KEY or is
generated once and persisted in the console data volume.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "va_session"
DEFAULT_SESSION_TTL = 12 * 3600  # 12 h


# ---------------------------------------------------------------------------
# cookie signing
# ---------------------------------------------------------------------------
def new_secret() -> str:
    """Random URL-safe secret (generated once when UI_SECRET_KEY is unset)."""
    return secrets.token_urlsafe(32)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_session(
    payload: dict, key: str, ttl: int = DEFAULT_SESSION_TTL, now: float | None = None
) -> str:
    """Serialize payload + expiry and sign it: '<body>.<hmac>'."""
    data = dict(payload)
    data["exp"] = int((now if now is not None else time.time()) + ttl)
    body = _b64encode(json.dumps(data, separators=(",", ":")).encode())
    signature = _b64encode(
        hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


def verify_session(token: str, key: str, now: float | None = None) -> dict | None:
    """Return the payload when signature and expiry check out, else None."""
    if not token or not key or "." not in token:
        return None
    body, _, signature = token.rpartition(".")
    if not body or not signature:
        return None
    expected = _b64encode(
        hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < (now if now is not None else time.time()):
        return None
    return payload


# ---------------------------------------------------------------------------
# local (email + password) auth
# ---------------------------------------------------------------------------
def local_auth_configured(env: dict) -> bool:
    """Local login is available when UI_PASSWORD is set (UI_EMAIL defaults)."""
    return bool(str(env.get("UI_PASSWORD", "") or "").strip())


def verify_login(email: str, password: str, env: dict) -> bool:
    """Constant-time check of an email/password pair against the env config."""
    expected_email = str(env.get("UI_EMAIL", "") or "").strip().lower()
    expected_password = str(env.get("UI_PASSWORD", "") or "")
    if not expected_email or not expected_password or not password:
        return False
    email_ok = hmac.compare_digest(
        (email or "").strip().lower().encode(), expected_email.encode()
    )
    password_ok = hmac.compare_digest(password.encode(), expected_password.encode())
    return email_ok and password_ok


# ---------------------------------------------------------------------------
# OIDC (SSO)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OidcConfig:
    """OIDC/OAuth2 settings, all from environment variables."""

    issuer_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_url: str = ""  # explicit override; empty = derive from request
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    allowed_emails: tuple[str, ...] = field(default_factory=tuple)
    allowed_domains: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env: dict) -> "OidcConfig":
        def _split(name: str) -> tuple[str, ...]:
            return tuple(
                item.strip().lower()
                for item in str(env.get(name, "") or "").split(",")
                if item.strip()
            )

        scopes = tuple(
            item.strip()
            for item in str(
                env.get("OIDC_SCOPES", "") or "openid profile email"
            ).split()
            if item.strip()
        )
        return cls(
            issuer_url=str(env.get("OIDC_ISSUER_URL", "") or "").strip().rstrip("/"),
            client_id=str(env.get("OIDC_CLIENT_ID", "") or "").strip(),
            client_secret=str(
                env.get("OIDC_CLIENT_SECRET", "")
                or env.get("OIDC_CLIENT_ID_SECRET", "")
                or ""
            ).strip(),
            redirect_url=str(env.get("OIDC_REDIRECT_URL", "") or "").strip(),
            scopes=scopes or ("openid", "profile", "email"),
            allowed_emails=_split("OIDC_ALLOWED_EMAILS"),
            allowed_domains=_split("OIDC_ALLOWED_DOMAINS"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.issuer_url and self.client_id and self.client_secret)

    def email_allowed(self, email: str) -> bool:
        """Apply the optional email/domain allow-list (empty = allow all)."""
        if not self.allowed_emails and not self.allowed_domains:
            return True
        email = (email or "").strip().lower()
        if not email:
            return False
        if email in self.allowed_emails:
            return True
        domain = email.rpartition("@")[2]
        return domain in self.allowed_domains


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------
def resolve_local_auth_mode(env: dict, oidc: OidcConfig) -> str:
    """Login-form mode: 'enabled' | 'disabled' | 'setup' (nothing configured)."""
    mode = str(env.get("UI_LOCAL_AUTH", "auto") or "auto").strip().lower()
    if mode in ("false", "off", "no", "0", "disabled"):
        return "disabled"
    if mode in ("true", "on", "yes", "1", "enabled"):
        return "enabled"
    # auto: the form is shown unless OIDC takes over; 'setup' = nothing set up
    if oidc.enabled:
        return "disabled"
    return "enabled" if local_auth_configured(env) else "setup"


def internal_token(env: dict) -> str:
    """Shared secret for the agent <-> console internal API.

    CONSOLE_INTERNAL_TOKEN wins when set; otherwise a stable value is derived
    from LIVEKIT_API_SECRET, which both containers already share, so the
    internal API is protected by default without extra configuration.
    """
    explicit = str(env.get("CONSOLE_INTERNAL_TOKEN", "") or "").strip()
    if explicit:
        return explicit
    secret = str(env.get("LIVEKIT_API_SECRET", "") or "")
    if not secret:
        return ""
    return hmac.new(
        secret.encode(), b"voice-assistant/console-internal", hashlib.sha256
    ).hexdigest()[:40]

