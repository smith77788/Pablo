"""«Это я» и «Завершить» попадают в тот сеанс, который на экране.

Жалоба владельца 21.11.2026: «после того как я нажимаю кнопку "Это я" —
ничего не происходит». Отпечаток сеанса Telegram — int64, а число в JSON
браузер читает как double: всё, что больше 2^53, приходит округлённым.
Экран отправлял назад ДРУГОЙ отпечаток, пометка ложилась на несуществующий
сеанс, и после перезагрузки списка сеанс оставался «чужим». Так же мимо
шло «Завершить». Сервер обязан отдавать отпечаток строкой.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

BIG = -8732164921342543217  # реальный порядок величины: |h| > 2^53


def _app(monkeypatch):
    from services import mini_app_api as api
    from services import profile_setter_engine as pse

    monkeypatch.setattr(api, "_get_uid", lambda request: 77)

    async def _row(pool, sql, *args):
        return {"id": 5, "session_str": "s" * 40, "trusted_session_hashes": []}

    monkeypatch.setattr(api, "_safe_fetchrow", _row)

    async def _list(session, acc):
        return {"ok": True, "own_created": 1_700_000_000,
                "sessions": [{"hash": BIG, "device": "iPhone", "app": "Telegram iOS"}]}

    monkeypatch.setattr(pse, "list_sessions", _list)
    from services import mini_app_accounts
    app = web.Application()
    mini_app_accounts.setup_routes(app, pool=object())
    return app


def _handler(app, path):
    for r in app.router.routes():
        info = r.resource.get_info() if r.resource else {}
        if r.method == "GET" and info.get("formatter") == path:
            return r.handler
    raise AssertionError(path)


def test_session_hash_reaches_the_screen_exactly(monkeypatch):
    app = _app(monkeypatch)
    h = _handler(app, "/api/miniapp/account/{acc_id}/sessions")
    req = make_mocked_request("GET", "/api/miniapp/account/5/sessions",
                              match_info={"acc_id": "5"})
    resp = asyncio.new_event_loop().run_until_complete(h(req))
    body = json.loads(resp.text)
    got = body["sessions"][0]["hash"]
    # Так его прочтёт браузер: число стало бы double и потеряло цифры.
    assert isinstance(got, str), "отпечаток сеанса уходит в браузер числом и округляется"
    assert int(got) == BIG
    assert float(BIG) != BIG, "контроль: такое число действительно не переживает double"
