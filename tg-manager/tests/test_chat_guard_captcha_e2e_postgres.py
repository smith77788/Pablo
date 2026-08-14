"""Капча «Модератора чатов»: хранилище ожиданий + фоновый сметатель (живой PG)."""
from __future__ import annotations

import asyncio
import glob
import os
import re
from types import SimpleNamespace

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

CHAT = -1001112223334
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _clean(pool):
    _run(pool.execute("DELETE FROM guard_captcha_pending WHERE chat_id=$1", CHAT))


class FakeBot:
    def __init__(self):
        self.banned = []
        self.unbanned = []
        self.deleted = []

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append(user_id)

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self.unbanned.append(user_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def test_add_get_resolve(pool):
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.captcha_add(pool, CHAT, 42, captcha_msg_id=777, timeout=120, action="kick"))
    got = _run(cg.captcha_get(pool, CHAT, 42))
    assert got and got["captcha_msg_id"] == 777 and got["action"] == "kick"
    # прошёл капчу → запись удалена, возвращена
    popped = _run(cg.captcha_resolve(pool, CHAT, 42))
    assert popped and popped["user_id"] == 42
    assert _run(cg.captcha_get(pool, CHAT, 42)) is None


def test_pop_expired_only_due(pool):
    from services import chat_guard as cg
    _clean(pool)
    # один уже просрочен (timeout=-5 → expires_at в прошлом), другой свежий
    _run(cg.captcha_add(pool, CHAT, 1, captcha_msg_id=10, timeout=-5, action="kick"))
    _run(cg.captcha_add(pool, CHAT, 2, captcha_msg_id=20, timeout=300, action="ban"))
    due = _run(cg.captcha_pop_expired(pool, limit=50))
    ids = {r["user_id"] for r in due}
    assert 1 in ids and 2 not in ids            # только просроченный
    assert _run(cg.captcha_get(pool, CHAT, 1)) is None   # удалён при pop
    assert _run(cg.captcha_get(pool, CHAT, 2)) is not None
    _clean(pool)


def test_sweeper_punishes_expired(pool):
    from services import chat_guard as cg, chat_guard_runner as runner
    _clean(pool)
    _run(cg.captcha_add(pool, CHAT, 55, captcha_msg_id=99, timeout=-1, action="kick"))
    _run(cg.captcha_add(pool, CHAT, 66, captcha_msg_id=98, timeout=-1, action="ban"))
    bot = FakeBot()
    n = _run(runner.sweep_once(pool, bot))
    assert n == 2
    # kick = бан+разбан, ban = только бан
    assert 55 in bot.banned and 55 in bot.unbanned
    assert 66 in bot.banned and 66 not in bot.unbanned
    # сообщения-капчи удалены
    assert 99 in bot.deleted and 98 in bot.deleted
    # записи вычищены
    assert _run(cg.captcha_get(pool, CHAT, 55)) is None
    assert _run(cg.captcha_get(pool, CHAT, 66)) is None
