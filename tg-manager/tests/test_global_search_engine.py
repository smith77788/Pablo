"""Global Search — раздел 12 паритета Telegram Expert.

Проверяем РЕАЛЬНОЕ исполнение конвейера поиска на мок-Telethon:
классификацию сущностей (channel/group/bot/user), дедуп, лимит, обработку
пустого запроса и FloodWait, и что mini-app маршрут зарегистрирован.
"""
from __future__ import annotations

import asyncio
import inspect
import types
from unittest.mock import AsyncMock, patch

from services import global_search_engine as gse


class _Ent:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Found:
    def __init__(self, chats, users):
        self.chats = chats
        self.users = users
        self.results = []
        self.my_results = []


def _fake_client(found):
    client = types.SimpleNamespace()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()

    async def _call(req):
        return found
    client.__call__ = _call
    # Telethon client is called as client(request); support both
    client_call = AsyncMock(return_value=found)
    client.side = client_call
    return client


def _run(coro):
    return asyncio.run(coro)


def test_empty_query_returns_error():
    res = _run(gse.search_public("sess", "  ", 10))
    assert res["ok"] is False and res["results"] == []


def test_classify_channel_group_bot_user():
    assert gse._classify(_Ent(broadcast=True)) == "channel"
    assert gse._classify(_Ent(broadcast=False, megagroup=True)) == "group"
    assert gse._classify(_Ent(broadcast=False, megagroup=False, bot=True)) == "bot"
    assert gse._classify(_Ent(broadcast=False, megagroup=False, bot=False)) == "user"


def test_search_parses_and_dedupes():
    chats = [
        _Ent(id=1, broadcast=True, title="Доставка Мск", username="dostavka_msk",
             participants_count=1200, verified=True, scam=False),
        _Ent(id=2, megagroup=True, broadcast=False, title="Чат доставки", username=None,
             participants_count=50, verified=False, scam=False),
    ]
    users = [
        _Ent(id=3, bot=True, first_name="Delivery", last_name="Bot", username="deliverybot"),
        _Ent(id=1, broadcast=True, title="Доставка Мск", username="dostavka_msk"),  # dup id+type
    ]
    found = _Found(chats, users)

    fake = types.SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
    call = AsyncMock(return_value=found)

    class _Client:
        connect = fake.connect
        disconnect = fake.disconnect
        def __call__(self, req):
            return call(req)

    with patch("services.account_manager._make_client", return_value=_Client()):
        res = _run(gse.search_public("enc-sess", "доставка", limit=20, _acc={"id": 9}))

    assert res["ok"] is True
    ids = [(r["type"], r["id"]) for r in res["results"]]
    # channel(1), group(2), bot(3); дубликат channel(1) отброшен
    assert ("channel", 1) in ids
    assert ("group", 2) in ids
    assert ("bot", 3) in ids
    assert len(res["results"]) == 3  # дедуп сработал
    ch = next(r for r in res["results"] if r["id"] == 1)
    assert ch["username"] == "dostavka_msk" and ch["participants"] == 1200 and ch["verified"] is True


def test_call_error_surfaced_not_swallowed():
    """Ошибка при вызове Telegram не глотается — возвращается ok=False + текст."""
    class _Client:
        connect = AsyncMock()
        disconnect = AsyncMock()
        def __call__(self, req):
            raise RuntimeError("USERNAME_INVALID")

    with patch("services.account_manager._make_client", return_value=_Client()):
        res = _run(gse.search_public("s", "x", 10, _acc={}))
    assert res["ok"] is False and "USERNAME_INVALID" in res["error"] and res["results"] == []


def test_route_registered():
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    assert 'app.router.add_post("/api/miniapp/global_search", global_search)' in src
    assert "async def global_search(" in src
