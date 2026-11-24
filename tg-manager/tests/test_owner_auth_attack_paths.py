"""A personal mirror key must never mint sessions for another owner."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from services import mini_app_api as api
from services import mirror_bot, owner_sessions
from services.mini_app_auth import parse_token

from tests.test_mirror_bot import MAIN, MIRROR, _make_init_data

OWNER = 42
VICTIM = 12345


class MirrorPool:
    async def fetchval(self, sql, *args):
        return MIRROR

    async def fetchrow(self, sql, *args):
        return {'owner_id': OWNER, 'token_enc': MIRROR}

    async def fetch(self, sql, *args):
        return [{'owner_id': OWNER, 'token_enc': MIRROR}]


def login(monkeypatch, user_id, mirror_hint):
    monkeypatch.setattr(api, '_bot_token', lambda: MAIN)
    monkeypatch.setattr(mirror_bot, 'decrypt_token', lambda token: token)
    opened = AsyncMock(return_value='test_session')
    monkeypatch.setattr(owner_sessions, 'open_session', opened)
    app = web.Application()
    api.setup_routes(app, MirrorPool())
    handler = next(route.handler for route in app.router.routes()
                   if route.method == 'POST' and route.resource.get_info().get('path') == '/api/miniapp/auth')
    body = {'initData': _make_init_data(MIRROR, user_id=user_id)}
    if mirror_hint:
        body['mirror'] = 7
    request = SimpleNamespace(json=AsyncMock(return_value=body), headers={})
    return asyncio.run(handler(request)), opened


@pytest.mark.parametrize('mirror_hint', [True, False])
def test_attacker_mirror_key_cannot_impersonate_another_owner(monkeypatch, mirror_hint):
    response, opened = login(monkeypatch, VICTIM, mirror_hint)
    assert response.status == 401
    opened.assert_not_awaited()


@pytest.mark.parametrize('mirror_hint', [True, False])
def test_own_mirror_login_still_works_and_records_actual_origin(monkeypatch, mirror_hint):
    response, opened = login(monkeypatch, OWNER, mirror_hint)
    assert response.status == 200
    data = json.loads(response.body)
    assert parse_token(data['token'], MAIN) == OWNER
    assert opened.await_args.args[2] == 'mirror'
