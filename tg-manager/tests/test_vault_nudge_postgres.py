"""Напоминания «подключите Хранилище диалогов» (services/vault_nudge.py) на
ЖИВОМ Postgres: кому и когда уходит, что второе напоминание не уходит раньше
срока, что подключившему и старой базе не пишется ничего, и что claim счётчика
не даёт двум проходам отправить одно и то же.

Запрос с make_interval, LEFT JOIN и ON CONFLICT ... WHERE проверяется только
живым драйвером. Без INFRAGRAM_TEST_DSN — skip (см. docstring
test_invite_e2e_postgres.py).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

NEW3 = 995101       # зарегистрирован 3 дня назад, не подключал
NEW1 = 995102       # 1 день назад — рано
OLD = 995103        # 40 дней назад — старая база, выкатка не пишет
CONNECTED = 995104  # 3 дня назад, но Хранилище подключено
WEEK = 995105       # 8 дней назад, одно напоминание 6 дней назад
WEEK_FRESH = 995106  # 8 дней назад, одно напоминание вчера — рано для второго
DONE = 995107       # 8 дней назад, оба напоминания ушли
IDS = [NEW3, NEW1, OLD, CONNECTED, WEEK, WEEK_FRESH, DONE]

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
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


async def _seed(pool):
    await pool.execute("DELETE FROM vault_nudges WHERE owner_id=ANY($1::bigint[])", IDS)
    await pool.execute("DELETE FROM business_connections WHERE owner_id=ANY($1::bigint[])", IDS)
    await pool.execute("DELETE FROM platform_users WHERE user_id=ANY($1::bigint[])", IDS)
    for uid, days in ((NEW3, 3), (NEW1, 1), (OLD, 40), (CONNECTED, 3),
                      (WEEK, 8), (WEEK_FRESH, 8), (DONE, 8)):
        await pool.execute(
            "INSERT INTO platform_users(user_id, registered_at, first_seen) "
            "VALUES ($1, now() - make_interval(days => $2), now() - make_interval(days => $2))",
            uid, days)
    await pool.execute(
        "INSERT INTO business_connections(connection_id, owner_id, is_enabled) "
        "VALUES ('conn_nudge_995104', $1, TRUE)", CONNECTED)
    for uid, cnt, ago in ((WEEK, 1, 6), (WEEK_FRESH, 1, 1), (DONE, 2, 1)):
        await pool.execute(
            "INSERT INTO vault_nudges(owner_id, sent_count, last_sent_at) "
            "VALUES ($1, $2, now() - make_interval(days => $3))", uid, cnt, ago)


@pytest.fixture
def sent(monkeypatch):
    from database import db
    calls = []

    async def _fake_notify(pool, bot, user_id, pref, text, reply_markup=None, *, dedup_key=None):
        calls.append((user_id, dedup_key, text))
        return True

    monkeypatch.setattr(db, "notify_if_enabled", _fake_notify)
    return calls


def _ours(calls):
    return sorted((u, k) for u, k, _t in calls if u in IDS)


def test_nudges_go_to_the_right_people_once(pool, sent):
    from services import vault_nudge
    _run(_seed(pool))
    _run(vault_nudge.check_once(pool, bot=None))
    assert _ours(sent) == [(NEW3, "vault_nudge_1"), (WEEK, "vault_nudge_2")]
    # Тексты разные: второе — «последнее напоминание».
    texts = {u: t for u, _k, t in sent}
    assert "последнее" in texts[WEEK] and "последнее" not in texts[NEW3]

    # Второй проход сразу следом — никому: первое у NEW3 занято, второе ему рано.
    sent.clear()
    _run(vault_nudge.check_once(pool, bot=None))
    assert _ours(sent) == []
    counts = dict(_run(pool.fetch(
        "SELECT owner_id, sent_count FROM vault_nudges WHERE owner_id=ANY($1::bigint[])", IDS)))
    assert counts[NEW3] == 1 and counts[WEEK] == 2 and counts[DONE] == 2
    assert CONNECTED not in counts and OLD not in counts and NEW1 not in counts


def test_claim_is_taken_by_one_pass_only(pool):
    from services import vault_nudge
    _run(_seed(pool))
    assert _run(vault_nudge._claim(pool, NEW1, 0)) is True
    # Второй процесс с тем же «ожидаемым» счётчиком не проходит.
    assert _run(vault_nudge._claim(pool, NEW1, 0)) is False
    assert _run(vault_nudge._claim(pool, NEW1, 1)) is True
    assert _run(vault_nudge._claim(pool, NEW1, 1)) is False
