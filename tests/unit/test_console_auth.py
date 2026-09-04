"""Tests for console auth primitives (ui/app/auth_core.py)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui" / "app"))

import auth_core as ac


def test_session_sign_and_verify_roundtrip():
    token = ac.sign_session({"sub": "a@b.c", "method": "local"}, "key1", ttl=60)
    payload = ac.verify_session(token, "key1")
    assert payload is not None
    assert payload["sub"] == "a@b.c"


def test_session_rejects_tamper_wrong_key_expiry():
    token = ac.sign_session({"sub": "a@b.c"}, "key1", ttl=60)
    assert ac.verify_session(token + "x", "key1") is None
    assert ac.verify_session(token, "other-key") is None
    expired = ac.sign_session({"sub": "a@b.c"}, "key1", ttl=60, now=time.time() - 120)
    assert ac.verify_session(expired, "key1") is None
    assert ac.verify_session("", "key1") is None
    assert ac.verify_session("garbage-no-dot", "key1") is None


def test_local_login_constant_time_path():
    env = {"UI_EMAIL": "Admin@Example.com", "UI_PASSWORD": "s3cret"}
    assert ac.verify_login("admin@example.com", "s3cret", env)  # case-insensitive email
    assert not ac.verify_login("admin@example.com", "wrong", env)
    assert not ac.verify_login("someone@else.com", "s3cret", env)
    assert not ac.verify_login("admin@example.com", "", env)
    # not configured -> never succeeds
    assert not ac.verify_login("admin@example.com", "s3cret", {})
    assert ac.local_auth_configured(env) is True
    assert ac.local_auth_configured({"UI_PASSWORD": ""}) is False


def test_oidc_config_parsing():
    cfg = ac.OidcConfig.from_env(
        {
            "OIDC_ISSUER_URL": "https://id.example.com/",
            "OIDC_CLIENT_ID": "console",
            "OIDC_CLIENT_SECRET": "s3cret",
            "OIDC_SCOPES": "openid email",
            "OIDC_ALLOWED_EMAILS": "A@x.com",
            "OIDC_ALLOWED_DOMAINS": "Example.org",
        }
    )
    assert cfg.enabled is True
    assert cfg.issuer_url == "https://id.example.com"
    assert cfg.scopes == ("openid", "email")
    assert cfg.email_allowed("a@x.com")
    assert cfg.email_allowed("anyone@example.org")
    assert not cfg.email_allowed("nope@other.com")
    assert not cfg.email_allowed("")

    empty = ac.OidcConfig.from_env({})
    assert empty.enabled is False
    assert empty.email_allowed("anyone@anywhere.com")  # no allow-list = all


def test_local_auth_mode_resolution():
    oidc_on = ac.OidcConfig(issuer_url="i", client_id="c", client_secret="s")
    oidc_off = ac.OidcConfig()
    assert ac.resolve_local_auth_mode({"UI_PASSWORD": "p"}, oidc_on) == "disabled"
    assert ac.resolve_local_auth_mode({"UI_PASSWORD": "p"}, oidc_off) == "enabled"
    assert ac.resolve_local_auth_mode({}, oidc_off) == "setup"
    assert ac.resolve_local_auth_mode({"UI_PASSWORD": "", "UI_LOCAL_AUTH": "true"}, oidc_off) == "enabled"
    assert ac.resolve_local_auth_mode({"UI_PASSWORD": "p", "UI_LOCAL_AUTH": "false"}, oidc_off) == "disabled"


def test_internal_token_explicit_or_derived_stable():
    derived1 = ac.internal_token({"LIVEKIT_API_SECRET": "sec"})
    derived2 = ac.internal_token({"LIVEKIT_API_SECRET": "sec"})
    assert derived1 == derived2 and len(derived1) == 40
    assert ac.internal_token({"LIVEKIT_API_SECRET": "sec"}) != ac.internal_token(
        {"LIVEKIT_API_SECRET": "other"}
    )
    assert (
        ac.internal_token({"LIVEKIT_API_SECRET": "sec", "CONSOLE_INTERNAL_TOKEN": "abc"})
        == "abc"
    )
    assert ac.internal_token({}) == ""
