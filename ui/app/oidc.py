"""Minimal OIDC (OAuth2 authorization-code) client for the console.

Uses only httpx + the standard library against any spec-compliant provider
(Authentik, Keycloak, Auth0, Dex, ...). ID-token signature verification is
delegated to the provider's userinfo endpoint (called server-to-server with
the exchanged access token), which avoids pulling a JOSE stack into the
image while still proving the identity to *us* over TLS.

All configuration comes from environment variables - see OidcConfig in
auth_core.py. When the console is served behind a path-prefix proxy
(Caddy `tls` profile under /console), set OIDC_REDIRECT_URL explicitly,
because the provider must know the exact public callback URL.
"""

from __future__ import annotations

import secrets

from auth_core import OidcConfig

DISCOVERY_PATH = "/.well-known/openid-configuration"
HTTP_TIMEOUT = 10.0


class OidcError(RuntimeError):
    """Any OIDC flow failure surfaced to the user as a login error."""


class OidcClient:
    """Stateless per-request helpers; provider metadata is cached in-process."""

    def __init__(self, config: OidcConfig) -> None:
        self.config = config
        self._metadata: dict | None = None

    # ------------------------------------------------------------------
    async def metadata(self) -> dict:
        """OpenID provider metadata (discovery), cached after first fetch."""
        if self._metadata:
            return self._metadata
        import httpx

        url = self.config.issuer_url + DISCOVERY_PATH
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(url)
                response.raise_for_status()
                document = response.json()
        except Exception as exc:  # noqa: BLE001 - flow failure
            raise OidcError(f"OIDC discovery failed ({url}): {exc}") from exc
        for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
            if not document.get(key):
                raise OidcError(f"OIDC discovery document missing '{key}'")
        self._metadata = document
        return document

    # ------------------------------------------------------------------
    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(24)

    def redirect_uri_for(self, request) -> str:  # noqa: ANN001 - starlette Request
        """Callback URL derived from the incoming request (or env override)."""
        if self.config.redirect_url:
            return self.config.redirect_url
        base = str(request.base_url).rstrip("/")
        return f"{base}/auth/oidc/callback"

    def authorization_url(self, metadata: dict, redirect_uri: str, state: str) -> str:
        from urllib.parse import urlencode

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}"
    # ------------------------------------------------------------------
    async def exchange_code(self, metadata: dict, redirect_uri: str, code: str) -> dict:
        """Exchange the authorization code for tokens; returns token response."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.post(
                    metadata["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                    },
                    headers={"Accept": "application/json"},
                )
                if response.status_code >= 400:
                    raise OidcError(
                        f"token endpoint returned HTTP {response.status_code}"
                    )
                return response.json()
        except OidcError:
            raise
        except Exception as exc:  # noqa: BLE001 - flow failure
            raise OidcError(f"token exchange failed: {exc}") from exc

    async def fetch_userinfo(self, metadata: dict, access_token: str) -> dict:
        """Fetch the authenticated identity from the userinfo endpoint."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(
                    metadata["userinfo_endpoint"],
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if response.status_code >= 400:
                    raise OidcError(
                        f"userinfo endpoint returned HTTP {response.status_code}"
                    )
                return response.json()
        except OidcError:
            raise
        except Exception as exc:  # noqa: BLE001 - flow failure
            raise OidcError(f"userinfo request failed: {exc}") from exc

    # ------------------------------------------------------------------
    async def complete_login(self, request, code: str) -> str:  # noqa: ANN001
        """Full flow backend: code -> verified email. Raises OidcError."""
        metadata = await self.metadata()
        redirect_uri = self.redirect_uri_for(request)
        tokens = await self.exchange_code(metadata, redirect_uri, code)
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise OidcError("token response contained no access_token")
        userinfo = await self.fetch_userinfo(metadata, access_token)
        email = str(userinfo.get("email") or "").strip()
        if not email:
            raise OidcError("provider did not return an email claim")
        if not self.config.email_allowed(email):
            raise OidcError(f"email {email} is not allowed to sign in")
        return email

