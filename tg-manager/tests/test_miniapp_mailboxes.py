"""Подключение почты в мини-аппе не раскрывает секреты и чужие ящики."""
import json
import smtplib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from services import mini_app_api, mini_app_mailboxes


def handlers(monkeypatch, pool, uid=42):
    monkeypatch.setattr(mini_app_api, '_get_uid', lambda request: uid)
    app = web.Application()
    mini_app_mailboxes.setup_routes(app, pool)
    return {route.method: route.handler for route in app.router.routes()
            if '/oauth/' not in (route.resource.get_info().get('formatter') or '')}


@pytest.mark.asyncio
async def test_list_only_reads_owners_public_metadata(monkeypatch):
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_ID', 'public-client-id')
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_SECRET', 'private-client-secret')
    monkeypatch.setenv('EMAIL_OAUTH_REDIRECT_URI', 'https://example.org/oauth/email/callback')
    pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    response = await handlers(monkeypatch, pool)['GET'](None)
    sql, owner = pool.fetch.call_args.args
    assert owner == 42 and 'WHERE owner_id=$1' in sql
    assert 'smtp_pass' not in sql and 'oauth_access_token' not in sql
    assert response.status == 200
    data = json.loads(response.text)['oauth'][0]
    assert data['client_id'] == 'public-client-id'
    assert data['redirect_uri'] == 'https://example.org/oauth/email/callback'
    assert 'private-client-secret' not in response.text


@pytest.mark.asyncio
async def test_connect_checks_before_encrypted_upsert(monkeypatch):
    pool = SimpleNamespace(execute=AsyncMock())
    checks = []
    monkeypatch.setattr(mini_app_mailboxes, 'check_credentials', lambda *args: checks.append(args))
    monkeypatch.setattr(mini_app_mailboxes, 'encrypt_token', lambda value: 'ENC:test')
    body = dict(provider='gmail', email='Me@example.com', password='secret-value')
    request = SimpleNamespace(json=AsyncMock(return_value=body))
    response = await handlers(monkeypatch, pool)['POST'](request)
    assert response.status == 200
    assert checks == [('smtp.gmail.com', 587, 'me@example.com', 'secret-value')]
    args = pool.execute.call_args.args
    assert args[1] == 42 and args[-1] == 'ENC:test'
    assert 'secret-value' not in response.text
    assert json.loads(response.text)['ok'] is True


@pytest.mark.asyncio
async def test_arbitrary_smtp_host_rejected(monkeypatch):
    pool = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(json=AsyncMock(return_value=dict(
        provider='127.0.0.1', email='me@example.com', password='secret')))
    response = await handlers(monkeypatch, pool)['POST'](request)
    assert response.status == 400
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_login_never_saves_secret_or_exposes_server_reply(monkeypatch):
    pool = SimpleNamespace(execute=AsyncMock())

    def reject(*args):
        raise smtplib.SMTPAuthenticationError(535, b'sensitive server response')

    monkeypatch.setattr(mini_app_mailboxes, 'check_credentials', reject)
    request = SimpleNamespace(json=AsyncMock(return_value=dict(
        provider='gmail', email='me@example.com', password='secret')))
    response = await handlers(monkeypatch, pool)['POST'](request)
    assert response.status == 400
    assert 'sensitive' not in response.text
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_is_owner_scoped(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    response = await handlers(monkeypatch, pool)['DELETE'](
        SimpleNamespace(match_info={'mailbox_id':'9'}))
    sql, mailbox_id, owner = pool.fetchrow.call_args.args
    assert 'owner_id=$2' in sql and (mailbox_id, owner) == (9, 42)
    assert response.status == 404


@pytest.mark.asyncio
async def test_unauthorized_never_reads_or_writes(monkeypatch):
    for method, handler in handlers(monkeypatch, object(), uid=None).items():
        assert (await handler(None)).status == 401, method
