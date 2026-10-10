"""Manager Mode по живому Postgres: сохранение токена + приём апдейта managed_bot.

Telegram-вызовы застаблены: get_me (валидация токена) и get_managed_bot_token
(в хендлере). Проверяем реальное подключение бота в managed_bots и уведомление.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
from types import SimpleNamespace

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 991701
OTHER = 991702
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


def _clean(pool, *bot_ids):
    _run(pool.execute("DELETE FROM managed_bots WHERE added_by = ANY($1::bigint[])",
                      [OWNER, OTHER]))
    if bot_ids:
        _run(pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                          list(bot_ids)))


def _stub_get_me(monkeypatch, mapping):
    """bot_api.get_me(http, token) → info по токену (или None)."""
    from services import bot_api

    async def fake_get_me(http, token):
        return mapping.get(token)

    monkeypatch.setattr(bot_api, "get_me", fake_get_me)


def test_store_managed_bot_connects(pool, monkeypatch):
    from services import managed_bots as mb
    _clean(pool, 5000001)
    _stub_get_me(monkeypatch, {
        "tok1": {"id": 5000001, "username": "child_bot", "first_name": "Child"}})
    res = _run(mb.store_managed_bot(pool, None, "tok1", OWNER, expected_bot_id=5000001))
    assert res["ok"] and res["added"] is True
    assert res["username"] == "child_bot" and res["bot_id"] == 5000001
    row = _run(pool.fetchrow(
        "SELECT username, added_by, created_via FROM managed_bots WHERE bot_id=$1", 5000001))
    assert row["username"] == "child_bot" and row["added_by"] == OWNER
    assert row["created_via"] == "managed"
    # повторное подключение тем же владельцем → added False (уже есть)
    res2 = _run(mb.store_managed_bot(pool, None, "tok1", OWNER, expected_bot_id=5000001))
    assert res2["ok"] and res2["added"] is False


def test_store_rejects_id_mismatch_and_bad_token(pool, monkeypatch):
    from services import managed_bots as mb
    _clean(pool, 5000002)
    _stub_get_me(monkeypatch, {
        "tokX": {"id": 5000002, "username": "x_bot", "first_name": "X"}})
    # id из getMe ≠ ожидаемого из апдейта → отказ, ничего не пишем
    res = _run(mb.store_managed_bot(pool, None, "tokX", OWNER, expected_bot_id=9999999))
    assert res["ok"] is False and res["reason"] == "id_mismatch"
    assert _run(pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1", 5000002)) == 0
    # невалидный токен (getMe вернул None)
    res2 = _run(mb.store_managed_bot(pool, None, "unknown", OWNER))
    assert res2["ok"] is False and res2["reason"] == "invalid_token"


def test_store_taken_by_other_owner(pool, monkeypatch):
    from services import managed_bots as mb
    _clean(pool, 5000003)
    _stub_get_me(monkeypatch, {
        "tokA": {"id": 5000003, "username": "a_bot", "first_name": "A"}})
    _run(mb.store_managed_bot(pool, None, "tokA", OWNER, expected_bot_id=5000003))
    # другой оператор создаёт тот же bot_id → 'taken'
    res = _run(mb.store_managed_bot(pool, None, "tokA", OTHER, expected_bot_id=5000003))
    assert res["ok"] and res["added"] == "taken"


class _FakeBot:
    def __init__(self, token=None, raise_token=False):
        self._token = token
        self._raise = raise_token
        self.sent = []

    async def get_managed_bot_token(self, user_id):
        if self._raise:
            raise RuntimeError("Bot Management Mode disabled")
        return self._token

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)


def _event(creator_id, bot_id, uname="new_bot"):
    return SimpleNamespace(
        user=SimpleNamespace(id=creator_id, full_name="Creator", username="cr"),
        bot_user=SimpleNamespace(id=bot_id, username=uname, first_name="New",
                                 is_bot=True))


def test_handler_connects_and_notifies(pool, monkeypatch):
    from bot.handlers import managed_bots as h
    _clean(pool, 5000004)
    _stub_get_me(monkeypatch, {
        "tok_ok": {"id": 5000004, "username": "new_bot", "first_name": "New"}})
    bot = _FakeBot(token="tok_ok")
    _run(h.on_managed_bot(_event(OWNER, 5000004), bot, pool, None))
    # бот подключён в базу + оператор уведомлён «создан и подключён»
    assert _run(pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1", 5000004)) == 1
    assert bot.sent and "подключ" in bot.sent[0][1].lower()


def test_handler_token_error_guides_user(pool, monkeypatch):
    from bot.handlers import managed_bots as h
    _clean(pool, 5000005)
    bot = _FakeBot(raise_token=True)   # Manager Mode не включён → токен не выдан
    _run(h.on_managed_bot(_event(OWNER, 5000005), bot, pool, None))
    # бота не подключили, но подсказали про Bot Management Mode
    assert _run(pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1", 5000005)) == 0
    assert bot.sent and "management mode" in bot.sent[0][1].lower()
