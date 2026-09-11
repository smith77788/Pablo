"""Защита от накрутки/ботов (flood_guard).

Чистые тесты детектора всплеска идут в CI. Полный путь (конфиг → эпизод →
пометка suspect → очистка → решение handle_new_user) — на живом Postgres.
"""
from __future__ import annotations

import os

import pytest

from services import flood_guard as fg


# ── Чистые ────────────────────────────────────────────────────────────────────
def test_is_attack():
    assert fg.is_attack(30, 30) is True
    assert fg.is_attack(31, 30) is True
    assert fg.is_attack(29, 30) is False
    # порог 0/отрицательный = защита неактивна
    assert fg.is_attack(1000, 0) is False
    assert fg.is_attack(1000, -5) is False


def test_record_new_user_window():
    fg.reset_window(12345)
    # 10 подписчиков в одно «мгновение» → скорость 10
    for i in range(10):
        r = fg.record_new_user(12345, now=1000.0)
    assert r == 10
    # спустя окно старые выпадают → счётчик падает
    r2 = fg.record_new_user(12345, now=1000.0 + fg.WINDOW_SEC + 1)
    assert r2 == 1
    fg.reset_window(12345)


# ── Живой Postgres ─────────────────────────────────────────────────────────────
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_flood_guard_full_postgres():
    import asyncio
    import asyncpg

    OWN, BOT = 802000, 558000
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM bot_flood_events WHERE bot_id=$1", BOT)
            await pool.execute("DELETE FROM bot_flood_config WHERE bot_id=$1", BOT)
            await pool.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
            fg.reset_window(BOT)
            fg._config_cache.pop(BOT, None)

        try:
            await _cl()
            await pool.execute(
                "INSERT INTO managed_bots(bot_id,token,added_by,is_active) "
                "VALUES($1,$2,$3,TRUE) ON CONFLICT (bot_id) DO NOTHING",
                BOT, "tkn:" + str(BOT), OWN)

            # по умолчанию защита off
            assert (await fg.get_config(pool, BOT))["mode"] == "off"

            # включаем protect с низким порогом
            cfg = await fg.set_config(pool, BOT, OWN, mode="protect", threshold_per_min=3)
            assert cfg["mode"] == "protect" and cfg["threshold_per_min"] == 3
            fg._config_cache.pop(BOT, None)  # сбросить кэш после set

            # эмулируем подписчиков; регистрируем в bot_users, прогоняем handle_new_user
            decisions = []
            for uid in range(1, 7):  # 6 новых — превысит порог 3
                await pool.execute(
                    "INSERT INTO bot_users(bot_id,user_id,first_seen,last_seen) "
                    "VALUES($1,$2,now(),now()) ON CONFLICT (bot_id,user_id) DO NOTHING",
                    BOT, 700000 + uid)
                decisions.append(await fg.handle_new_user(pool, BOT, OWN, 700000 + uid))

            # первые (до порога) — не всплеск; после порога — suspect в protect
            assert decisions[0]["attack"] is False
            assert decisions[-1]["attack"] is True
            assert decisions[-1]["suspect"] is True
            assert decisions[-1]["block"] is False  # protect ≠ block

            # эпизод открыт, есть помеченные
            ep = await fg.active_episode(pool, BOT)
            assert ep and ep["status"] == "active" and ep["suspected_count"] >= 1
            sc = await fg.suspect_count(pool, BOT)
            assert sc >= 1

            # мягкая очистка → suspects деактивируются (вон из аудитории)
            purged = await fg.purge_suspects(pool, BOT, hard=False)
            assert purged == sc
            n_active_suspect = await pool.fetchval(
                "SELECT count(*) FROM bot_users WHERE bot_id=$1 AND suspect AND is_active",
                BOT)
            assert n_active_suspect == 0

            # снятие флагов
            unflagged = await fg.unflag_all(pool, BOT)
            assert unflagged == sc
            assert await fg.suspect_count(pool, BOT) == 0

            # block-режим: решение содержит block=True при всплеске
            await fg.set_config(pool, BOT, OWN, mode="block", threshold_per_min=1)
            fg._config_cache.pop(BOT, None)
            fg.reset_window(BOT)
            await pool.execute(
                "INSERT INTO bot_users(bot_id,user_id,first_seen,last_seen) "
                "VALUES($1,$2,now(),now()) ON CONFLICT (bot_id,user_id) DO NOTHING",
                BOT, 799999)
            d = await fg.handle_new_user(pool, BOT, OWN, 799999)
            assert d["attack"] and d["block"] is True and d["suspect"] is True

            # выключение защиты → нейтральное решение, окно сброшено
            await fg.set_config(pool, BOT, OWN, mode="off")
            fg._config_cache.pop(BOT, None)
            d2 = await fg.handle_new_user(pool, BOT, OWN, 799998)
            assert d2["mode"] == "off" and d2["attack"] is False and d2["suspect"] is False
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_end_stale_episodes_postgres():
    """Фоновый свип закрывает протухшие активные эпизоды (последний всплеск давно)."""
    import asyncio
    import asyncpg

    OWN, BOT = 802003, 558003
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM bot_flood_events WHERE bot_id=$1", BOT)
            # свежий активный эпизод — не трогаем
            await pool.execute(
                "INSERT INTO bot_flood_events(bot_id,owner_id,last_at,status) "
                "VALUES($1,$2,now(),'active')", BOT, OWN)
            # протухший активный (последний всплеск час назад) — должен закрыться
            await pool.execute(
                "INSERT INTO bot_flood_events(bot_id,owner_id,last_at,status) "
                "VALUES($1,$2,now()-interval '1 hour','active')", BOT, OWN)
            ended = await fg.end_stale_episodes(pool)
            assert ended >= 1
            active = await pool.fetchval(
                "SELECT count(*) FROM bot_flood_events WHERE bot_id=$1 AND status='active'",
                BOT)
            assert active == 1  # остался только свежий
            await pool.execute("DELETE FROM bot_flood_events WHERE bot_id=$1", BOT)
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_suspects_excluded_from_audience_postgres():
    """Архитектурный гейт: помеченные suspect не попадают в аудиторию рассылок."""
    import asyncio
    import asyncpg
    from database import db as _db

    OWN, BOT = 802002, 558002
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
        try:
            await _cl()
            await pool.execute(
                "INSERT INTO managed_bots(bot_id,token,added_by,is_active) "
                "VALUES($1,$2,$3,TRUE) ON CONFLICT (bot_id) DO NOTHING",
                BOT, "tkn:" + str(BOT), OWN)
            # 2 обычных + 3 накрученных (suspect)
            for uid in range(2):
                await pool.execute(
                    "INSERT INTO bot_users(bot_id,user_id,is_active) VALUES($1,$2,TRUE)",
                    BOT, 810000 + uid)
            for uid in range(3):
                await pool.execute(
                    "INSERT INTO bot_users(bot_id,user_id,is_active,suspect,flagged_at) "
                    "VALUES($1,$2,TRUE,TRUE,now())", BOT, 820000 + uid)
            # аудитория считает только обычных
            assert await _db.get_audience_count(pool, BOT) == 2
            ids = await _db.get_audience_user_ids(pool, BOT)
            assert set(ids) == {810000, 810001}
            # снятие флагов возвращает их в аудиторию
            await fg.unflag_all(pool, BOT)
            assert await _db.get_audience_count(pool, BOT) == 5
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_flag_recent_and_hard_purge_postgres():
    import asyncio
    import asyncpg

    OWN, BOT = 802001, 558001
    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM bot_users WHERE bot_id=$1", BOT)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT)
        try:
            await _cl()
            await pool.execute(
                "INSERT INTO managed_bots(bot_id,token,added_by,is_active) "
                "VALUES($1,$2,$3,TRUE) ON CONFLICT (bot_id) DO NOTHING",
                BOT, "tkn:" + str(BOT), OWN)
            # 3 недавних + 1 старый
            for uid in range(3):
                await pool.execute(
                    "INSERT INTO bot_users(bot_id,user_id,first_seen,last_seen) "
                    "VALUES($1,$2,now(),now())", BOT, 800000 + uid)
            await pool.execute(
                "INSERT INTO bot_users(bot_id,user_id,first_seen,last_seen) "
                "VALUES($1,$2,now()-interval '2 hours',now())", BOT, 899999)
            # заблокировать волну за 5 минут → только 3 недавних
            n = await fg.flag_recent(pool, BOT, 5)
            assert n == 3
            assert await fg.suspect_count(pool, BOT) == 3
            # жёсткая очистка удаляет строки
            purged = await fg.purge_suspects(pool, BOT, hard=True)
            assert purged == 3
            left = await pool.fetchval(
                "SELECT count(*) FROM bot_users WHERE bot_id=$1", BOT)
            assert left == 1  # старый остался
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
