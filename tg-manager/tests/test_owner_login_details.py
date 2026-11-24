"""Identity, scoped inspection, and honest owner-session failures."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from services import owner_sessions as sessions


def test_alert_identifies_owner_and_distinguishes_fleet():
    text = sessions.build_alert({'label': '<device>'}, 'telegram', 'device', owner_id=42)
    assert 'tg://user?id=42' in text
    assert 'Telegram ID 42' in text
    assert 'панель Infragram' in text and 'подключённого Telegram-аккаунта' in text
    assert '&lt;device&gt;' in text


def test_details_escape_fields_and_never_disclose_credentials():
    text = sessions.session_details({'id': 7, 'kind': 'telegram',
        'device_label': '<script>', 'ip': '<bad>', 'country': 'NL',
        'sid': 'SECRET_TOKEN', 'pairing_id': 'SECRET_KEY', 'revoked_at': 'yes'}, 42)
    assert '№7' in text and 'завершён' in text
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert 'SECRET' not in text and '&lt;bad&gt;' in text


def test_alert_contains_read_and_revoke_actions(monkeypatch):
    import aiogram
    from database import db
    from services import owner_safeword

    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(aiogram, 'Bot', MagicMock(return_value=bot))
    notify = AsyncMock()
    monkeypatch.setattr(db, 'notify_if_enabled', notify)
    monkeypatch.setattr(owner_safeword, 'line', AsyncMock(return_value='\nВаше слово: тест'))
    pool = SimpleNamespace(fetchval=AsyncMock(return_value=7))
    asyncio.run(sessions._alert(pool, 42, 'private_sid', 'telegram', {'label': 'Phone'}, 'device'))
    assert notify.await_count == 1
    args = notify.await_args
    assert 'Telegram ID 42' in args.args[4]
    buttons = [b for line in args.kwargs['reply_markup'].inline_keyboard for b in line]
    assert {b.callback_data for b in buttons} == {'osess:view:7', 'osess:kill:7'}


@pytest.mark.parametrize('revoked', [None, 'yesterday'])
def test_view_is_scoped_and_read_only(monkeypatch, revoked):
    from bot.callbacks import OwnSessCb
    from bot.handlers import account_shield_hub as hub

    monkeypatch.setattr(hub, 'safe_answer', AsyncMock())
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={
        'id': 7, 'kind': 'telegram', 'device_label': 'Phone', 'revoked_at': revoked}))
    callback = SimpleNamespace(from_user=SimpleNamespace(id=42), message=SimpleNamespace(answer=AsyncMock()))
    asyncio.run(hub.cb_owner_session_view(callback, OwnSessCb(action='view', session_id=7), pool))
    sql, owner, session_id = pool.fetchrow.await_args.args
    assert 'owner_id=$1 AND id=$2' in sql and (owner, session_id) == (42, 7)
    assert 'sid' not in sql
    args = callback.message.answer.await_args
    assert 'Phone' in args.args[0]
    buttons = args.kwargs['reply_markup'].inline_keyboard
    assert bool(buttons) == (revoked is None)


def test_foreign_or_missing_session_reveals_no_details(monkeypatch):
    from bot.callbacks import OwnSessCb
    from bot.handlers import account_shield_hub as hub

    monkeypatch.setattr(hub, 'safe_answer', AsyncMock())
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    callback = SimpleNamespace(from_user=SimpleNamespace(id=99), message=SimpleNamespace(answer=AsyncMock()))
    asyncio.run(hub.cb_owner_session_view(callback, OwnSessCb(action='view', session_id=7), pool))
    assert 'не найден' in callback.message.answer.await_args.args[0]
    assert pool.fetchrow.await_args.args[1:] == (99, 7)


def test_db_failure_does_not_turn_into_empty_sessions():
    pool = SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError):
        asyncio.run(sessions.list_sessions(pool, 42, strict=True))
    assert asyncio.run(sessions.list_sessions(pool, 42)) == []


def test_api_returns_503_when_session_inventory_is_unavailable(monkeypatch):
    from aiohttp import web
    from services import mini_app_api as api

    monkeypatch.setattr(api, '_get_uid', lambda request: 42)
    monkeypatch.setattr(api, '_current_sid', lambda request: 'private')
    app = web.Application()
    pool = SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError('offline')))
    api.setup_routes(app, pool)
    handler = next(route.handler for route in app.router.routes()
                   if route.method == 'GET' and route.resource.get_info().get('path') == '/api/miniapp/owner/sessions')
    response = asyncio.run(handler(SimpleNamespace()))
    assert response.status == 503
    assert 'sessions' not in response.text
