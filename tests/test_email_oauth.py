from __future__ import annotations

import importlib
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))


def _module(monkeypatch):
    monkeypatch.setenv("MANAGER_BOT_TOKEN", "123:test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_SECRET", "secret")
    return importlib.import_module("services.email_oauth")


def test_state_roundtrip(monkeypatch) -> None:
    email_oauth = _module(monkeypatch)
    monkeypatch.setenv("EMAIL_OAUTH_STATE_SECRET", "state-secret")

    state = email_oauth.make_state(391641532, "google")
    payload = email_oauth.parse_state(state)

    assert payload["owner_id"] == 391641532
    assert payload["provider"] == "google"


def test_state_rejects_tampering(monkeypatch) -> None:
    email_oauth = _module(monkeypatch)
    monkeypatch.setenv("EMAIL_OAUTH_STATE_SECRET", "state-secret")

    state = email_oauth.make_state(1, "google")
    raw, sig = state.split(".", 1)
    bad_raw = raw[:-1] + ("a" if raw[-1] != "a" else "b")
    bad_state = f"{bad_raw}.{sig}"

    try:
        email_oauth.parse_state(bad_state)
    except ValueError:
        pass
    else:
        raise AssertionError("tampered state must be rejected")


def test_build_auth_url_includes_provider_scope_and_redirect(monkeypatch) -> None:
    email_oauth = _module(monkeypatch)
    monkeypatch.setenv("EMAIL_OAUTH_STATE_SECRET", "state-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bot.example")

    url = email_oauth.build_auth_url(1, "google")

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=google-client" in url
    assert "redirect_uri=https%3A%2F%2Fbot.example%2Foauth%2Femail%2Fcallback" in url
    assert "https%3A%2F%2Fmail.google.com%2F" in url


def test_missing_provider_settings_lists_required_env(monkeypatch) -> None:
    email_oauth = _module(monkeypatch)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("EMAIL_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    missing = email_oauth.missing_provider_settings("google")

    assert missing == [
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "EMAIL_OAUTH_REDIRECT_URI or PUBLIC_BASE_URL",
    ]
    assert email_oauth.is_provider_configured("google") is False
