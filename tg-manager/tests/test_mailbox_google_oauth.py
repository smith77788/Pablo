"""Быстрый вход в почту: привязка владельца, браузера и шифрование токенов."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from services import email_oauth as oauth
from services import mini_app_api, mini_app_mailboxes
from services.token_vault import decrypt_token, encrypt_token


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        'EMAIL_OAUTH_STATE_SECRET': 'test-state-secret',
        'TOKEN_ENCRYPTION_KEY': 'test-encryption-key',
        'GOOGLE_OAUTH_CLIENT_ID': 'test-client',
        'GOOGLE_OAUTH_CLIENT_SECRET': 'test-client-secret',
        'EMAIL_OAUTH_REDIRECT_URI': 'https://example.org/oauth/email/callback',
    }.items():
        monkeypatch.setenv(key, value)


def test_browser_handoff_has_unique_signed_state_and_pkce(configured):
    url = oauth.build_miniapp_auth_url(42, 'google')
    state = parse_qs(urlsplit(url).query)['state'][0]
    assert oauth.parse_state(state)['owner_id'] == 42
    provider = parse_qs(urlsplit(oauth._provider_auth_url('google', state)).query)
    assert provider['code_challenge_method'] == ['S256']
    expected = oauth._b64(hashlib.sha256(oauth._pkce_verifier(state).encode()).digest())
    assert provider['code_challenge'] == [expected]
    assert oauth.make_state(42, 'google') != oauth.make_state(42, 'google')
    raw, signature = state.split('.')
    with pytest.raises(ValueError):
        oauth.parse_state(raw + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:])


@pytest.mark.asyncio
async def test_callback_refuses_other_browser(configured, monkeypatch):
    exchange = AsyncMock(return_value='me@example.org')
    monkeypatch.setattr(oauth, 'exchange_code_and_save', exchange)
    app = web.Application()
    oauth.add_routes(app, object(), SimpleNamespace(send_message=AsyncMock()))
    handler = next(r.handler for r in app.router.routes()
                   if r.method == 'GET' and r.resource.get_info().get('path') == '/oauth/email/callback')
    state = oauth.make_state(42, 'google', flow='miniapp')
    request = SimpleNamespace(query={'code':'code', 'state':state}, cookies={})
    response = await handler(request)
    assert response.status == 400
    exchange.assert_not_called()
    request.cookies['ig_email_oauth'] = hashlib.sha256(state.encode()).hexdigest()
    response = await handler(request)
    assert response.status == 200
    assert exchange.call_args.args[1:3] == (42, 'google')
    assert exchange.call_args.kwargs['code_verifier'] == oauth._pkce_verifier(state)


@pytest.mark.asyncio
async def test_exchange_stores_only_encrypted_tokens(configured, monkeypatch):
    monkeypatch.setattr(oauth, '_request_token', AsyncMock(return_value={
        'access_token':'access-secret', 'refresh_token':'refresh-secret',
        'scope':'https://mail.google.com/', 'expires_in':3600,
    }))
    monkeypatch.setattr(oauth, '_fetch_profile', AsyncMock(return_value={
        'email':'me@example.org', 'verified_email':True,
    }))
    pool = SimpleNamespace(execute=AsyncMock())
    assert await oauth.exchange_code_and_save(pool, 42, 'google', 'code') == 'me@example.org'
    args = pool.execute.call_args.args
    assert args[1] == 42
    assert 'access-secret' not in args and 'refresh-secret' not in args
    assert decrypt_token(args[6]) == 'refresh-secret'
    assert decrypt_token(args[7]) == 'access-secret'


@pytest.mark.asyncio
async def test_cached_encrypted_access_token_is_decrypted(configured):
    mailbox = dict(oauth_provider='google', oauth_access_token=encrypt_token('access'),
                   oauth_expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert await oauth.get_access_token(None, mailbox) == 'access'


@pytest.mark.asyncio
async def test_oauth_start_uses_authenticated_owner(configured, monkeypatch):
    monkeypatch.setattr(mini_app_api, '_get_uid', lambda request: 42)
    app = web.Application()
    mini_app_mailboxes.setup_routes(app, object())
    handler = next(r.handler for r in app.router.routes()
                   if r.method == 'POST' and r.resource.get_info().get('formatter')
                   == '/api/miniapp/mailboxes/oauth/{provider}')
    response = await handler(SimpleNamespace(match_info={'provider':'google'}))
    state = parse_qs(urlsplit(json.loads(response.text)['url']).query)['state'][0]
    assert oauth.parse_state(state)['owner_id'] == 42
