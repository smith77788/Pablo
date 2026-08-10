"""Перехватчик ключей: юнит-матч + сквозной поллинг по живому Postgres.

Логика курсора/идемпотентности/доставки — на реальной БД (ON CONFLICT, JSONB
keywords, счётчики). Telegram-чтение заглушено через fetch-seam, поэтому проверяем
именно поведение перехватчика, а не сеть.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

from services import keyword_watcher as kw


# ── юнит: матч и нормализация (без БД) ───────────────────────────────────────

class TestMatch:
    def test_normalize_dedup_lower(self):
        assert kw.normalize_keywords("Куплю, продам,КУПЛЮ") == ["куплю", "продам"]
        assert kw.normalize_keywords(["  Ищу ", "ищу", ""]) == ["ищу"]

    def test_match_case_insensitive_substring(self):
        assert kw.match_keywords("Срочно КУПЛЮ квартиру", ["куплю"]) == "куплю"
        assert kw.match_keywords("продаю авто", ["куплю", "продаю"]) == "продаю"

    def test_no_match(self):
        assert kw.match_keywords("привет всем", ["куплю"]) is None
        assert kw.match_keywords("", ["куплю"]) is None
        assert kw.match_keywords("текст", []) is None


# ── e2e на живом Postgres ────────────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 991401
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append((int(chat_id), text))
        return True


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
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


def _seed_watcher(pool, keywords=("куплю", "ищу")):
    async def _s():
        await pool.execute("DELETE FROM keyword_watchers WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        acc = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name) "
            "VALUES($1,'+79990001122','sess',TRUE,'active','A') RETURNING id", OWNER)
        wid = await kw.create_watcher(pool, OWNER, acc, "@targetchat", list(keywords), "Target")
        return acc, wid
    return _run(_s())


def _watcher(pool, wid):
    return _run(pool.fetchrow("SELECT * FROM keyword_watchers WHERE id=$1", wid))


def test_poll_records_matches_advances_cursor_and_delivers(pool):
    _acc, wid = _seed_watcher(pool)
    msgs = [
        {"message_id": 10, "from_user_id": 1, "from_username": "u1", "text": "всем привет"},
        {"message_id": 11, "from_user_id": 2, "from_username": "u2", "text": "куплю щенка"},
        {"message_id": 12, "from_user_id": 3, "from_username": None, "text": "ищу репетитора"},
    ]

    async def fake_fetch(pool_, watcher, min_id, limit):
        return [m for m in msgs if m["message_id"] > min_id]

    bot = _FakeBot()
    res = _run(kw.poll_watcher(pool, bot, dict(_watcher(pool, wid)), fetch=fake_fetch))
    assert res == {"messages": 3, "hits": 2, "skipped": False}
    # курсор доехал до максимума
    assert _watcher(pool, wid)["last_msg_id"] == 12
    assert _watcher(pool, wid)["hits_count"] == 2
    # лиды доставлены оператору
    assert len(bot.sent) == 2 and all(s[0] == OWNER for s in bot.sent)
    delivered = _run(pool.fetchval(
        "SELECT COUNT(*) FROM keyword_hits WHERE watcher_id=$1 AND delivered=TRUE", wid))
    assert delivered == 2


def test_poll_is_idempotent_no_duplicate_hits(pool):
    _acc, wid = _seed_watcher(pool)
    msgs = [{"message_id": 5, "from_user_id": 1, "from_username": "u", "text": "куплю авто"}]

    async def fake_fetch(pool_, watcher, min_id, limit):
        # намеренно всегда возвращаем то же сообщение (эмуляция повторного/сбойного поллинга)
        return list(msgs)

    bot = _FakeBot()
    r1 = _run(kw.poll_watcher(pool, bot, dict(_watcher(pool, wid)), fetch=fake_fetch))
    r2 = _run(kw.poll_watcher(pool, bot, dict(_watcher(pool, wid)), fetch=fake_fetch))
    assert r1["hits"] == 1
    assert r2["hits"] == 0  # то же сообщение второй раз лидом не становится
    total = _run(pool.fetchval("SELECT COUNT(*) FROM keyword_hits WHERE watcher_id=$1", wid))
    assert total == 1
    assert len(bot.sent) == 1  # повторно оператору не шлём


def test_quarantined_account_is_skipped(pool):
    _acc, wid = _seed_watcher(pool)

    called = {"n": 0}

    async def fake_fetch(pool_, watcher, min_id, limit):
        called["n"] += 1
        return []

    import services.infra_memory as im
    orig = im.is_account_quarantined

    async def always_quarantined(pool_, acc_id, **k):
        return True
    im.is_account_quarantined = always_quarantined
    try:
        res = _run(kw.poll_watcher(pool, _FakeBot(), dict(_watcher(pool, wid)), fetch=fake_fetch))
    finally:
        im.is_account_quarantined = orig
    assert res["skipped"] is True
    assert called["n"] == 0  # до чтения чата не дошли


def test_create_watcher_validates_input(pool):
    _acc, wid = _seed_watcher(pool)
    acc = _watcher(pool, wid)["account_id"]
    with pytest.raises(ValueError):
        _run(kw.create_watcher(pool, OWNER, acc, "@chat", []))       # нет ключей
    with pytest.raises(ValueError):
        _run(kw.create_watcher(pool, OWNER, acc, "  ", ["куплю"]))   # нет чата
