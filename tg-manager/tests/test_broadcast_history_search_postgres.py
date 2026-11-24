"""Поиск по истории рассылок на живом Postgres.

История рассылок — список без поиска: срез по статусу сужал её до сотни
строк, а дальше «ту рассылку про скидку» искали прокруткой. Теперь есть
`q=`: ищем по тексту сообщения и по нику бота, то есть по двум вещам,
которые владелец помнит.

Нужна живая база, а не заглушка: условие собирается строкой с номерами
параметров ($1, $2, …) и джойном к `managed_bots`, а `_safe_fetch` глушит
ошибку запроса и отдаёт пустой список — на заглушке сломанный запрос
выглядел бы как «ничего не найдено».
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995411
STRANGER = 995412
BOT_A = 880099411
BOT_B = 880099412


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed(pool):
    await _clean(pool)
    for bot_id, username, owner in ((BOT_A, "skidka_shop_bot", OWNER),
                                    (BOT_B, "news_channel_bot", OWNER)):
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,$3,TRUE,$4)",
            f"1234:search-test-{bot_id}", bot_id, username, owner)
    rows = (
        (BOT_A, "Скидка 30% до пятницы", "completed", OWNER),
        (BOT_A, "Напоминаем про вебинар", "completed", OWNER),
        (BOT_B, "Новый выпуск уже вышел", "completed", OWNER),
        (BOT_B, "Чужая скидка — не наша", "completed", STRANGER),
    )
    for bot_id, text, status, owner in rows:
        await pool.execute(
            "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
            "VALUES($1,$2,10,$3,$4)", bot_id, text, status, owner)


async def _clean(pool):
    await pool.execute("DELETE FROM broadcasts WHERE bot_id=ANY($1::bigint[])",
                       [BOT_A, BOT_B])
    await pool.execute("DELETE FROM managed_bots WHERE bot_id=ANY($1::bigint[])",
                       [BOT_A, BOT_B])


def _ask(monkeypatch, query: str, uid: int = OWNER) -> dict:
    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api, mini_app_broadcast

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: uid)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await _seed(pool)
            app = web.Application()
            mini_app_broadcast.setup_routes(app, pool)
            handler = next(
                r.handler for r in app.router.routes()
                if getattr(r.resource, "canonical", "") == "/api/miniapp/broadcasts")
            resp = await handler(
                make_mocked_request("GET", "/api/miniapp/broadcasts" + query))
            return json.loads(resp.body)
        finally:
            await _clean(pool)
            await pool.close()

    return _run(_go())


def test_search_finds_by_message_text(monkeypatch):
    data = _ask(monkeypatch, "?q=скидка")
    texts = [b["message_text"] for b in data["broadcasts"]]
    assert texts == ["Скидка 30% до пятницы"], data
    assert data["q"] == "скидка"


def test_search_finds_by_bot_username(monkeypatch):
    data = _ask(monkeypatch, "?q=news_channel")
    assert [b["message_text"] for b in data["broadcasts"]] == ["Новый выпуск уже вышел"]


def test_found_counts_the_search_and_not_the_whole_history(monkeypatch):
    """«Показано N из M» под поиском обязано считать найденное.

    Иначе экран печатал бы «1 из 3», где 3 не связано с тем, что на экране,
    и `has_more` обещал бы ещё страницу, которой нет.
    """
    data = _ask(monkeypatch, "?q=скидка")
    assert data["found"] == 1, data
    assert data["page"]["has_more"] is False
    plain = _ask(monkeypatch, "")
    assert plain["found"] is None, "без поиска найденного нет — есть вся история"
    assert plain["total"] >= 3


def test_search_never_reaches_another_owner(monkeypatch):
    """У чужой рассылки тот же бот и то же слово — скоуп держит запрос."""
    data = _ask(monkeypatch, "?q=скидка", uid=STRANGER)
    assert [b["message_text"] for b in data["broadcasts"]] == ["Чужая скидка — не наша"]
    mine = _ask(monkeypatch, "?q=скидка")
    assert all("Чужая" not in b["message_text"] for b in mine["broadcasts"]), mine
