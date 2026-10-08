"""Виртуальный слой на живом Postgres: распад и остывание каскада доезжают до базы.

Чистые функции проверены в test_virtual_layer_decay_catches_up; здесь — что
записи реально ложатся в `virtual_states`: заглушка пула не проверяет типы
связывания, а слой пишет `expires_at` (timestamptz) и читает его в условии
выборки. Проверяем то, за чем этот слой и строился:

  * просроченное на месяц «готов купить» за ОДИН проход доходит до дна и
    отдаёт срок — то есть уходит из окна выборки и перестаёт занимать её
    лимит (500 строк на всех владельцев за редкий проход);
  * каскад снимает с аудитории прежнее «горячая», когда готовых не осталось;
  * список «кого дожимать первыми» не показывает просроченные рунги.

Без INFRAGRAM_TEST_DSN — скип. Инструкция по стенду — в
tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 771903
_NOW = datetime.now(timezone.utc)


async def _pool():
    # lock_timeout: применение схемы другим прогоном держит блокировку, и без
    # потолка ожидание выглядит как зависший тест (разбор — в памяти силоса).
    return await asyncpg.create_pool(_DSN, min_size=1, max_size=3,
                                     server_settings={"lock_timeout": "3000"})


async def _clean(pool):
    await pool.execute("DELETE FROM virtual_state_history WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM virtual_states WHERE owner_id=$1", OWNER)


async def _put(pool, entity_type, entity_id, value, *, expires_at=None,
               source=None, state_key="funnel", confidence=0.8):
    await pool.execute(
        "INSERT INTO virtual_states(owner_id, entity_type, entity_id, state_key, "
        "value, confidence, source, expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8) "
        "ON CONFLICT (owner_id, entity_type, entity_id, state_key) DO UPDATE "
        "SET value=EXCLUDED.value, expires_at=EXCLUDED.expires_at",
        OWNER, entity_type, str(entity_id), state_key, value, confidence,
        source, expires_at)


async def _row(pool, entity_type, entity_id, state_key="funnel"):
    return await pool.fetchrow(
        "SELECT value, expires_at FROM virtual_states WHERE owner_id=$1 "
        "AND entity_type=$2 AND entity_id=$3 AND state_key=$4",
        OWNER, entity_type, str(entity_id), state_key)


@pytest.mark.asyncio
async def test_month_of_silence_lands_at_the_bottom_in_one_pass():
    from services import virtual_layer as V

    pool = await _pool()
    try:
        await _clean(pool)
        await _put(pool, V.USER, "c-cold", "ready",
                   expires_at=_NOW - timedelta(days=30), source="bot_7")
        n = await V.run_decay(pool, OWNER)
        assert n == 1, f"распад не доехал до базы: остыло {n}"
        r = await _row(pool, V.USER, "c-cold")
        assert r["value"] == "new", (
            f"за проход опустились только до «{r['value']}»: месяц тишины "
            "продолжает читаться как интерес")
        assert r["expires_at"] is None, (
            "срок на дне не снят — строка останется в окне выборки распада "
            "навсегда и будет занимать её лимит")
        # Второй проход этой строкой уже не занимается.
        assert await V.run_decay(pool, OWNER) == 0
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_stale_bottom_rows_stop_crowding_out_the_decay_window():
    """Главное следствие: лимит прохода достаётся тем, кому распад нужен.

    Так выглядела база через месяц работы: строки на дне с просроченным сроком
    копятся, выборка возвращает их первыми, и живой «готов купить» в лимит уже
    не попадает. Берём лимит 3 — он меньше числа мусорных строк.
    """
    from services import virtual_layer as V

    pool = await _pool()
    try:
        await _clean(pool)
        for i in range(5):
            await _put(pool, V.USER, f"c-junk{i}", "new",
                       expires_at=_NOW - timedelta(days=40 + i))
        await _put(pool, V.USER, "c-live", "ready",
                   expires_at=_NOW - timedelta(hours=2))
        # Первый проход разбирает мусор (срок снимается, остывших нет).
        await V.run_decay(pool, OWNER, limit=3)
        await V.run_decay(pool, OWNER, limit=3)
        # Теперь в окне остался только живой — он и остывает.
        await V.run_decay(pool, OWNER, limit=3)
        r = await _row(pool, V.USER, "c-live")
        assert r["value"] == "qualified", (
            f"живое состояние так и не остыло (осталось «{r['value']}»): "
            "мусорные строки на дне забрали лимит прохода")
        left = await pool.fetchval(
            "SELECT COUNT(*) FROM virtual_states WHERE owner_id=$1 "
            "AND expires_at IS NOT NULL AND expires_at <= now()", OWNER)
        assert left == 0, f"в окне распада осталось {left} строк, которым там не место"
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_cascade_cools_the_audience_down_in_the_database():
    from services import virtual_layer as V

    pool = await _pool()
    try:
        await _clean(pool)
        for i in range(30):
            await _put(pool, V.USER, f"c-hot{i}", "ready",
                       expires_at=_NOW + timedelta(hours=5))
        for i in range(70):
            await _put(pool, V.USER, f"c-new{i}", "new")
        v = await V.recompute_cascade(pool, OWNER, V.NETWORK, "audience", V.USER,
                                      min_count=20, min_share=0.15)
        assert v == "hot", f"аудитория не разогрелась: {v}"

        # Те же люди молчат месяц: сырое значение в базе не изменилось.
        await pool.execute(
            "UPDATE virtual_states SET expires_at=now() - INTERVAL '30 days' "
            "WHERE owner_id=$1 AND value='ready'", OWNER)
        v = await V.recompute_cascade(pool, OWNER, V.NETWORK, "audience", V.USER,
                                      min_count=20, min_share=0.15)
        assert v == V.COLD, (
            f"аудитория осталась «{v}»: каскад читает сырое значение и срок "
            "игнорирует, поэтому «горячая» держится на пропавших людях")
        assert (await _row(pool, V.NETWORK, "audience"))["value"] == V.COLD
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_overview_hot_list_leaves_out_expired_rungs():
    from services import virtual_layer as V

    pool = await _pool()
    try:
        await _clean(pool)
        await _put(pool, V.USER, "c-live", "ready",
                   expires_at=_NOW + timedelta(hours=5), confidence=0.7)
        await _put(pool, V.USER, "c-stale", "ready",
                   expires_at=_NOW - timedelta(days=21), confidence=0.95)
        ov = await V.overview(pool, OWNER)
        ids = [h["entity_id"] for h in ov["hot"]]
        assert "c-live" in ids
        assert "c-stale" not in ids, (
            "человек, пропавший три недели назад, стоит первым в списке "
            "«кого дожимать»: по такому списку нельзя работать")
    finally:
        await _clean(pool)
        await pool.close()
