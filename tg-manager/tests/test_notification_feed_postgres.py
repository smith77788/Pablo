"""Лента уведомлений мини-аппа: запись, чтение, отметка, чужой владелец.

Уведомления жили только сообщениями в личке бота: промотал чат — истории нет
нигде. Теперь каждое уведомление, прошедшее настройки владельца, попадает в
`notification_feed` (одна дверь — `db.notify_if_enabled`), а мини-апп
показывает ленту со счётчиком непрочитанного.

Живая база обязательна: заглушка пула не ловит ни связывание `$2::bigint[]`,
ни то, что `read_at IS NULL` действительно отбирает строки, — а именно на этом
классе ошибок продукт ломался молча (CLAUDE.md).

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5432/ci pytest …
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990861, 990862
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M
        from services import mini_app_notifications as NF

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: int(r.headers.get("X-Uid", OWNER))

        async def clean():
            await pool.execute(
                "DELETE FROM notification_feed WHERE owner_id = ANY($1::bigint[])",
                [OWNER, STRANGER])
        try:
            await clean()
            app = web.Application()
            NF.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool)
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            try:
                await clean()
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@live
def test_notification_lands_in_the_feed_and_can_be_read():
    from database import db

    async def body(client, pool):
        # Запись идёт тем же путём, что в проде: через _feed_record, которую
        # зовёт notify_if_enabled сразу после проверки настроек.
        await db._feed_record(pool, OWNER, "notify_ops",
                              "<b>Операция завершена</b>\nПриглашено 12 из 20", None)
        await db._feed_record(pool, OWNER, "notify_error",
                              "⚠️ Аккаунт ограничен", "acc:1")
        await db._feed_record(pool, STRANGER, "notify_ops", "чужое", None)

        d = await (await client.get("/api/miniapp/notifications")).json()
        assert d["unread"] == 2, d
        assert [i["title"] for i in d["items"]] == ["⚠️ Аккаунт ограничен", "Операция завершена"]
        # Разметку Telegram снимает СЕРВЕР: экран вставляет текст как текст.
        assert "<b>" not in d["items"][1]["title"]
        assert d["items"][1]["body"] == "Приглашено 12 из 20"
        assert d["items"][1]["kind"] == "Операции" and d["items"][1]["icon"] == "⚙️"
        assert all(i["unread"] for i in d["items"])

        # Чужая лента в выдачу не попадает ни строкой.
        assert all("чужое" != i["title"] for i in d["items"])

        # Отбор по типу события.
        one = await (await client.get("/api/miniapp/notifications?pref=notify_error")).json()
        assert len(one["items"]) == 1 and one["items"][0]["pref"] == "notify_error"
        # Счётчик непрочитанного — по ВСЕЙ ленте, а не по срезу.
        assert one["unread"] == 2

        # Отметка одной строки.
        nid = d["items"][0]["id"]
        r = await (await client.post("/api/miniapp/notifications/read",
                                     json={"ids": [nid]})).json()
        assert r["unread"] == 1
        only = await (await client.get("/api/miniapp/notifications?unread=1")).json()
        assert [i["id"] for i in only["items"]] == [d["items"][1]["id"]]

        # «Прочитать всё».
        assert (await (await client.post("/api/miniapp/notifications/read",
                                         json={"all": True})).json())["unread"] == 0
    _run(body)


@live
def test_stranger_cannot_read_or_mark_someones_feed():
    from database import db

    async def body(client, pool):
        await db._feed_record(pool, OWNER, "notify_ops", "мой отчёт", None)
        mine = await (await client.get("/api/miniapp/notifications")).json()
        nid = mine["items"][0]["id"]

        stranger = {"X-Uid": str(STRANGER)}
        d = await (await client.get("/api/miniapp/notifications", headers=stranger)).json()
        assert d["items"] == [] and d["unread"] == 0

        # Отметка чужой строки по её id ничего не меняет: скоуп держит ЗАПРОС.
        await client.post("/api/miniapp/notifications/read",
                          headers=stranger, json={"ids": [nid]})
        await client.post("/api/miniapp/notifications/read",
                          headers=stranger, json={"all": True})
        assert (await (await client.get("/api/miniapp/notifications")).json())["unread"] == 1
    _run(body)


@live
def test_feed_record_never_breaks_a_notification():
    """Запись в ленту fail-soft: уведомление дороже строки истории."""
    from database import db

    async def body(client, pool):
        class _Broken:
            async def execute(self, *a, **kw):
                raise RuntimeError("база недоступна")

        await db._feed_record(_Broken(), OWNER, "notify_ops", "текст", None)  # не бросает
        # Длинный текст режется, а не роняет вставку.
        await db._feed_record(pool, OWNER, "notify_ops", "я" * 5000, None)
        d = await (await client.get("/api/miniapp/notifications")).json()
        assert len(d["items"]) == 1
        assert len(d["items"][0]["title"]) <= 120
    _run(body)


def test_plain_text_keeps_the_owner_text():
    """Снятие разметки не должно съедать текст, который сам похож на тег."""
    from services.mini_app_notifications import plain_text, title_and_body

    assert plain_text("<b>Итог</b> 5/10") == "Итог 5/10"
    assert plain_text("цена &lt; 100 &amp; больше") == "цена < 100 & больше"
    assert plain_text('<a href="https://t.me/x">канал</a>') == "канал"
    assert title_and_body("Заголовок\nтело\nещё") == ("Заголовок", "тело\nещё")
    assert title_and_body("") == ("Уведомление", "")
