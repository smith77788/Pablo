"""Containment: a URL ticket must not outlive its revoked owner session."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from services import mini_app_api as api
from services import mini_app_auth as auth
from services import owner_sessions

KEY = '1234567890:AAtesttesttesttesttesttesttesttestt'


@pytest.mark.parametrize('path', ['/api/miniapp/accounts', '/api/miniapp/events'])
def test_revoking_session_also_revokes_its_url_tickets(monkeypatch, path):
    monkeypatch.setattr(api, '_bot_token', lambda: KEY)
    sid = 'compromised_ticket_session'
    owner_sessions._REVOKED.discard(sid)
    app = web.Application()
    api.setup_routes(app, SimpleNamespace())
    handler = next(route.handler for route in app.router.routes()
                   if route.method == 'POST'
                   and route.resource.get_info().get('path') == '/api/miniapp/url_ticket')

    class Request:
        def __init__(self):
            self.headers = {'Authorization': 'Bearer ' + auth.make_token(42, KEY, sid=sid)}
            self.query = {}
            self.method = 'POST'

        async def json(self):
            return {'path': path}

    response = asyncio.run(handler(Request()))
    assert response.status == 200
    ticket = json.loads(response.text)['ticket']
    request = SimpleNamespace(headers={}, method='GET', path=path, query={'ticket': ticket})
    assert api._uid_from_credentials(request) == 42
    owner_sessions._REVOKED.add(sid)
    try:
        assert api._uid_from_credentials(request) is None, (
            'URL ticket still authorizes account access after the originating session was revoked')
    finally:
        owner_sessions._REVOKED.discard(sid)


def test_bound_ticket_cannot_change_owner_session_path_or_version():
    path = '/api/miniapp/accounts'
    ticket = auth.make_url_ticket(42, KEY, path, sid='owner_session')
    assert auth.parse_url_ticket_sid(ticket, KEY, path) == (42, 'owner_session')
    assert auth.parse_url_ticket(ticket, KEY, path) == 42
    uid, stamp, sid, sig = ticket.split('.')
    for forged in (f'43.{stamp}.{sid}.{sig}', f'{uid}.{stamp}.other.{sig}',
                   f'{uid}.{stamp}.{sig}'):
        assert auth.parse_url_ticket_sid(forged, KEY, path) == (None, None)
    assert auth.parse_url_ticket_sid(ticket, KEY, '/api/miniapp/events') == (None, None)


def test_bound_ticket_expires_and_rejects_future_date(monkeypatch):
    now = 1700000000
    monkeypatch.setattr(auth.time, 'time', lambda: now)
    ticket = auth.make_url_ticket(42, KEY, '/api/miniapp/accounts', sid='owner_session')
    monkeypatch.setattr(auth.time, 'time', lambda: now - 1)
    assert auth.parse_url_ticket_sid(ticket, KEY, '/api/miniapp/accounts') == (None, None)
    monkeypatch.setattr(auth.time, 'time', lambda: now + auth.URL_TICKET_TTL + 1)
    assert auth.parse_url_ticket_sid(ticket, KEY, '/api/miniapp/accounts') == (None, None)


def test_legacy_ticket_remains_distinguishable():
    ticket = auth.make_url_ticket(42, KEY, '/api/miniapp/accounts')
    assert auth.parse_url_ticket_sid(ticket, KEY, '/api/miniapp/accounts') == (42, None)


@pytest.mark.parametrize('sid', ['', 'bad.sid', 'bad:session', 'x' * 129])
def test_invalid_session_identifiers_cannot_be_signed(sid):
    with pytest.raises(ValueError):
        auth.make_url_ticket(42, KEY, '/api/miniapp/accounts', sid=sid)


def test_unbound_url_ticket_is_not_an_http_credential(monkeypatch):
    monkeypatch.setattr(api, '_bot_token', lambda: KEY)
    path = '/api/miniapp/accounts'
    ticket = auth.make_url_ticket(42, KEY, path)
    req = SimpleNamespace(headers={}, method='GET', path=path, query={'ticket': ticket})
    assert api._uid_from_credentials(req) is None


def test_revoked_session_gets_terminal_response_instead_of_renewal(monkeypatch):
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(api, '_bot_token', lambda: KEY)
    sid = 'revoked_session_test'
    app = web.Application()
    api.setup_routes(app, SimpleNamespace())
    middleware = next(m for m in app.middlewares if m.__name__ == 'owner_revocation_middleware')
    request = make_mocked_request('POST', '/api/miniapp/auth', headers={
        'Authorization': 'Bearer ' + auth.make_token(42, KEY, sid=sid)})
    handler = AsyncMock()
    owner_sessions._REVOKED.add(sid)
    try:
        response = asyncio.run(middleware(request, handler))
        assert response.status == 403
        assert json.loads(response.text)['code'] == 'session_revoked'
        handler.assert_not_awaited()
    finally:
        owner_sessions._REVOKED.discard(sid)
