"""OAuth helpers for connecting email accounts via Gmail or Microsoft."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailOAuthProvider:
    name: str
    label: str
    client_id_env: str
    client_secret_env: str
    auth_url: str
    token_url: str
    profile_url: str
    scopes: tuple[str, ...]
    smtp_host: str
    smtp_port: int = 587


PROVIDERS: dict[str, EmailOAuthProvider] = {
    "google": EmailOAuthProvider(
        name="google",
        label="Google / Gmail",
        client_id_env="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        profile_url="https://www.googleapis.com/oauth2/v2/userinfo",
        scopes=("https://mail.google.com/", "openid", "email", "profile"),
        smtp_host="smtp.gmail.com",
    ),
    "microsoft": EmailOAuthProvider(
        name="microsoft",
        label="Microsoft / Outlook",
        client_id_env="MICROSOFT_OAUTH_CLIENT_ID",
        client_secret_env="MICROSOFT_OAUTH_CLIENT_SECRET",
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        profile_url="https://graph.microsoft.com/v1.0/me",
        scopes=("offline_access", "openid", "profile", "email", "SMTP.Send"),
        smtp_host="smtp-mail.outlook.com",
    ),
}


def redirect_uri() -> str:
    configured = os.getenv("EMAIL_OAUTH_REDIRECT_URI", "").strip()
    if configured:
        return configured
    public_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base:
        return f"{public_base}/oauth/email/callback"
    return ""


def is_provider_configured(provider_name: str) -> bool:
    provider = PROVIDERS.get(provider_name)
    if not provider:
        return False
    return not missing_provider_settings(provider_name)


def missing_provider_settings(provider_name: str) -> list[str]:
    provider = PROVIDERS.get(provider_name)
    if not provider:
        return ["unknown provider"]

    missing: list[str] = []
    if not os.getenv(provider.client_id_env, "").strip():
        missing.append(provider.client_id_env)
    if not os.getenv(provider.client_secret_env, "").strip():
        missing.append(provider.client_secret_env)
    if not redirect_uri():
        missing.append("EMAIL_OAUTH_REDIRECT_URI or PUBLIC_BASE_URL")
    return missing


def provider_status() -> dict[str, bool]:
    return {name: is_provider_configured(name) for name in PROVIDERS}


def _secret() -> bytes:
    secret = (
        os.getenv("EMAIL_OAUTH_STATE_SECRET", "").strip()
        or os.getenv("ADMIN_SECRET", "").strip()
        or os.getenv("WEBHOOK_SECRET", "").strip()
        or os.getenv("MANAGER_BOT_TOKEN", "").strip()
    )
    return secret.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def make_state(owner_id: int, provider_name: str) -> str:
    payload = {
        "owner_id": owner_id,
        "provider": provider_name,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def parse_state(state: str, max_age_s: int = 900) -> dict[str, Any]:
    try:
        raw_b64, sig_b64 = state.split(".", 1)
        raw = _unb64(raw_b64)
        expected = hmac.new(_secret(), raw, hashlib.sha256).digest()
        actual = _unb64(sig_b64)
    except Exception as exc:
        raise ValueError("invalid state") from exc
    if not hmac.compare_digest(expected, actual):
        raise ValueError("invalid state signature")
    payload = json.loads(raw)
    issued_at = int(payload.get("iat", 0) or 0)
    if not issued_at or time.time() - issued_at > max_age_s:
        raise ValueError("expired state")
    return payload


def build_auth_url(owner_id: int, provider_name: str) -> str:
    provider = PROVIDERS[provider_name]
    client_id = os.getenv(provider.client_id_env, "").strip()
    state = make_state(owner_id, provider_name)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
    }
    if provider_name == "google":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    return f"{provider.auth_url}?{urlencode(params)}"


async def _request_token(
    session: aiohttp.ClientSession,
    provider: EmailOAuthProvider,
    payload: dict[str, str],
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.post(provider.token_url, data=payload, timeout=timeout) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"token exchange failed: {resp.status} {text[:200]}")
        data = await resp.json(content_type=None)
        return data


async def _fetch_profile(
    session: aiohttp.ClientSession,
    provider: EmailOAuthProvider,
    access_token: str,
) -> dict[str, Any]:
    async with session.get(
        provider.profile_url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"profile fetch failed: {resp.status} {text[:200]}")
        data = await resp.json(content_type=None)
        return data


def _profile_email(provider_name: str, profile: dict[str, Any]) -> str:
    if provider_name == "microsoft":
        return str(
            profile.get("mail")
            or profile.get("userPrincipalName")
            or profile.get("email")
            or ""
        ).lower()
    return str(profile.get("email") or "").lower()


async def exchange_code_and_save(
    pool: asyncpg.Pool,
    owner_id: int,
    provider_name: str,
    code: str,
) -> str:
    provider = PROVIDERS[provider_name]
    client_id = os.getenv(provider.client_id_env, "").strip()
    client_secret = os.getenv(provider.client_secret_env, "").strip()
    async with aiohttp.ClientSession() as session:
        token = await _request_token(
            session,
            provider,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri(),
            },
        )
        access_token = str(token["access_token"])
        refresh_token = str(token.get("refresh_token") or "")
        expires_in = int(token.get("expires_in", 3600) or 3600)
        profile = await _fetch_profile(session, provider, access_token)

    email = _profile_email(provider_name, profile)
    if not email:
        raise RuntimeError("provider did not return an email address")
    if not refresh_token:
        raise RuntimeError(
            "provider did not return refresh token; reconnect with consent"
        )
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, expires_in - 60)
    )
    await pool.execute(
        """INSERT INTO strike_email_accounts
               (owner_id, email, smtp_host, smtp_port, smtp_pass, auth_type,
                oauth_provider, oauth_refresh_token, oauth_access_token,
                oauth_expires_at, oauth_scopes)
           VALUES ($1,$2,$3,$4,'','oauth',$5,$6,$7,$8,$9)
           ON CONFLICT (owner_id, email)
           DO UPDATE SET smtp_host=$3, smtp_port=$4, smtp_pass='',
                         auth_type='oauth', oauth_provider=$5,
                         oauth_refresh_token=$6, oauth_access_token=$7,
                         oauth_expires_at=$8, oauth_scopes=$9,
                         is_active=TRUE, fail_count=0""",
        owner_id,
        email,
        provider.smtp_host,
        provider.smtp_port,
        provider.name,
        refresh_token,
        access_token,
        expires_at,
        list(provider.scopes),
    )
    return email


async def get_access_token(pool: asyncpg.Pool, email_account: dict[str, Any]) -> str:
    provider_name = str(email_account.get("oauth_provider") or "")
    provider = PROVIDERS.get(provider_name)
    if not provider:
        raise RuntimeError("unknown oauth provider")
    access_token = str(email_account.get("oauth_access_token") or "")
    expires_at = email_account.get("oauth_expires_at")
    if access_token and expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
            return access_token

    refresh_token = str(email_account.get("oauth_refresh_token") or "")
    if not refresh_token:
        raise RuntimeError("missing oauth refresh token")
    async with aiohttp.ClientSession() as session:
        token = await _request_token(
            session,
            provider,
            {
                "client_id": os.getenv(provider.client_id_env, "").strip(),
                "client_secret": os.getenv(provider.client_secret_env, "").strip(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    access_token = str(token["access_token"])
    expires_in = int(token.get("expires_in", 3600) or 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, expires_in - 60)
    )
    await pool.execute(
        """UPDATE strike_email_accounts
           SET oauth_access_token=$1, oauth_expires_at=$2
           WHERE id=$3""",
        access_token,
        expires_at,
        email_account["id"],
    )
    return access_token


def add_routes(app: web.Application, pool: asyncpg.Pool, bot: Bot) -> None:
    async def callback(request: web.Request) -> web.Response:
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        if not code or not state:
            return web.Response(status=400, text="Missing code/state")
        try:
            payload = parse_state(state)
            owner_id = int(payload["owner_id"])
            provider_name = str(payload["provider"])
            email = await exchange_code_and_save(pool, owner_id, provider_name, code)
        except Exception as exc:
            log_exc_swallow(log, "email oauth callback failed")
            return web.Response(status=400, text=f"OAuth failed: {exc}")
        try:
            await bot.send_message(
                owner_id,
                f"✅ Email OAuth подключён: <code>{email}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return web.Response(text="Email OAuth connected. You can close this page.")

    app.router.add_get("/oauth/email/callback", callback)
