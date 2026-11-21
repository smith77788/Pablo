"""«🗑 Отключить бота» из уведомления об отозванном токене находит бота.

Бот с отозванным токеном система снимает из работы сама (is_active=FALSE,
deleted_at остаётся пустым), а потом присылает владельцу уведомление с
кнопкой «Отключить бота». Кнопка вела в cb_delete, а он искал бота через
db.get_bot, который видел только работающих (is_active=TRUE). Итог: алерт
«Бот не найден», и убрать мёртвого бота из списка Mini App (там видны и
снятые из работы) было нечем.

Теперь подтверждение ищет запись по внутреннему id, включая снятую из
работы, и только у владельца (удалять может только он — как и delete_bot).
Telegram при этом не спрашивается: бот там недоступен.

Тест падает без фикса: без include_inactive запрос по-прежнему требует
is_active=TRUE, и обработчик отвечает «Бот не найден».
"""
from __future__ import annotations

import asyncio
import os
import re
from types import SimpleNamespace

import pytest

OWNER = 992771
BOT = 992772
DSN = os.getenv("INFRAGRAM_TEST_DSN")


class _RowPool:
    """Отвечает строкой бота так, как ответила бы база на ЭТОТ текст запроса.

    Снятый из работы бот (is_active=FALSE) возвращается, только если запрос
    разрешает неактивных ($4 = include_inactive) — то есть проверяем и текст
    условия, и переданный довод, а не одно имя аргумента.
    """

    def __init__(self, *, is_active: bool, added_by: int = OWNER):
        self.row = {"bot_id": BOT, "added_by": added_by, "is_active": is_active,
                    "deleted_at": None, "username": "Max_Akkbot",
                    "first_name": "Max", "token": "x"}
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, q, *a):
        q = " ".join(q.split())
        self.queries.append((q, a))
        assert "FROM managed_bots" in q
        bot_id, uid = a[0], a[1]
        owner_only = a[2] if len(a) > 2 else False
        include_inactive = a[3] if len(a) > 3 else False
        if bot_id != self.row["bot_id"]:
            return None
        allows_inactive = bool(re.search(r"is_active=TRUE OR \$4::boolean", q)) and include_inactive
        if not self.row["is_active"] and not allows_inactive:
            return None
        if owner_only and self.row["added_by"] != uid:
            return None
        if self.row["added_by"] != uid and not owner_only:
            # в этом стенде у чужого нет ни экосистемы, ни workspace
            return None
        return dict(self.row)


class _Msg:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text, **_):
        self.edits.append(text)


class _Cb:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.message = _Msg()
        self.alerts: list[str] = []

    async def answer(self, text=None, *a, **k):
        if text:
            self.alerts.append(text)


def _press(pool, uid):
    from bot.callbacks import BotCb
    from bot.handlers import bots as h

    cb = _Cb(uid)
    asyncio.run(h.cb_delete(cb, BotCb(action="delete", bot_id=BOT), pool))
    return cb


def test_disable_button_finds_bot_taken_out_of_work_by_dead_token():
    cb = _press(_RowPool(is_active=False), OWNER)
    assert not any("не найден" in a for a in cb.alerts), cb.alerts
    assert cb.message.edits and "Удалить бота" in cb.message.edits[0]
    assert "сохранятся" in cb.message.edits[0]


def test_disable_button_still_works_for_active_bot():
    cb = _press(_RowPool(is_active=True), OWNER)
    assert cb.message.edits and "Удалить бота" in cb.message.edits[0]


def test_disable_button_is_owner_only():
    cb = _press(_RowPool(is_active=False), OWNER + 1)
    assert cb.alerts == ["Бот не найден."]
    assert cb.message.edits == []


def test_get_bot_default_still_hides_inactive():
    from database import db

    pool = _RowPool(is_active=False)
    assert asyncio.run(db.get_bot(pool, BOT, OWNER)) is None


@pytest.mark.skipif(not DSN, reason="нужен живой PostgreSQL")
def test_dead_bot_disable_end_to_end_postgres():
    """Живая база: снятый из работы бот находится, отключается, данные целы."""
    import asyncpg
    from database import db

    async def scenario():
        async with asyncpg.create_pool(DSN, min_size=1, max_size=2) as pool:
            await pool.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
            try:
                await pool.execute(
                    "INSERT INTO managed_bots (bot_id, added_by, token, first_name, "
                    "username, is_active, last_error, fail_streak) "
                    "VALUES($1,$2,'1:dead','Max','Max_Akkbot',FALSE,'unauthorized',3)",
                    BOT, OWNER)
                await pool.execute(
                    "INSERT INTO bot_users (bot_id, user_id) VALUES($1, 1)", BOT)
                assert await db.get_bot(pool, BOT, OWNER) is None
                row = await db.get_bot(pool, BOT, OWNER, owner_only=True,
                                       include_inactive=True)
                assert row and row["bot_id"] == BOT
                assert await db.get_bot(pool, BOT, OWNER + 1, owner_only=True,
                                        include_inactive=True) is None
                assert await db.delete_bot(pool, BOT, OWNER)
                assert await pool.fetchval(
                    "SELECT deleted_at IS NOT NULL FROM managed_bots WHERE bot_id=$1", BOT)
                assert await pool.fetchval(
                    "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", BOT) == 1
                assert await db.get_bot(pool, BOT, OWNER, owner_only=True,
                                        include_inactive=True) is None
            finally:
                await pool.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
                await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)

    asyncio.run(scenario())
