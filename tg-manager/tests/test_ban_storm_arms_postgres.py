"""Волна банов на живом Postgres: предохранитель Anti-Storm реально взводится.

Это проверка того самого дефекта, из-за которого модуль «защита флота от волны
банов» не видел ни одного бана: `anti_storm` считает РАЗНЫЕ аккаунты с
критическим ограничением за 30 минут по `restriction_events`, а хук бана
`op_worker._on_account_banned` в эту таблицу не писал — писали только
shadowban_monitor и drift_detector. Заглушка пула такое не покажет: здесь
важны и запись, и то, что её находит СЛЕДУЮЩИЙ запрос с окном и DISTINCT.

Без INFRAGRAM_TEST_DSN — скип. Инструкция по стенду — в
tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 664201


async def _pool():
    # lock_timeout: применение схемы другим прогоном держит блокировку, и без
    # потолка ожидание выглядит как зависший тест.
    return await asyncpg.create_pool(_DSN, min_size=1, max_size=3,
                                     server_settings={"lock_timeout": "3000"})


async def _clean(pool):
    await pool.execute(
        "DELETE FROM restriction_events WHERE owner_id=$1 OR account_id IN "
        "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
    await pool.execute("DELETE FROM account_flood_log WHERE account_id IN "
                       "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
    await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    for table in ("organism_state", "organism_events"):
        try:
            await pool.execute(f"DELETE FROM {table} WHERE owner_id=$1", OWNER)
        except Exception:
            pass


async def _fleet(pool, n: int) -> list[int]:
    ids = []
    for i in range(n):
        ids.append(await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES($1, $2, 'sess', TRUE) RETURNING id",
            OWNER, f"+7901{OWNER}{i:02d}"))
    return ids


@pytest.mark.asyncio
async def test_a_wave_of_bans_puts_the_fleet_to_sleep():
    from services import anti_storm, op_worker

    pool = await _pool()
    try:
        await _clean(pool)
        ids = await _fleet(pool, 12)

        assert (await anti_storm.current(pool, OWNER))["level"] == "calm"

        # Telegram прокатил чистку: пять аккаунтов за минуты.
        for acc_id in ids[:5]:
            await pool.execute(
                "UPDATE tg_accounts SET is_active=FALSE, acc_status='banned' "
                "WHERE id=$1", acc_id)
            await op_worker._on_account_banned(pool, OWNER, acc_id, "invite")

        hit = await anti_storm._distinct_hit(pool, OWNER)
        assert hit == 5, (
            f"предохранитель видит {hit} пострадавших аккаунтов вместо пяти: "
            "хук бана не кладёт сигнал в restriction_events, и защита от "
            "волны банов не может сработать на бане никогда")

        st = await anti_storm.current(pool, OWNER)
        assert st["level"] == "storm", f"шторм не объявлен: {st}"
        assert st["mult"] >= anti_storm.DEEP_SLEEP_MULT, st
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_three_bans_raise_the_watch_level_not_the_deep_sleep():
    """Три бана — насторожиться (темп вдвое), а не усыплять флот.

    Флот из девяти: на маленьком флоте доля не применяется (иначе два бана из
    трёх читались бы как шторм), решает только абсолютный порог.
    """
    from services import anti_storm, op_worker

    pool = await _pool()
    try:
        await _clean(pool)
        ids = await _fleet(pool, 9)
        for acc_id in ids[:3]:
            await op_worker._on_account_banned(pool, OWNER, acc_id, "bulk_op")
        st = await anti_storm.current(pool, OWNER)
        assert st["level"] == "watch", st
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_the_same_account_banned_twice_is_one_signal():
    """Хук дёргается на каждой операции, налетевшей на аккаунт."""
    from services import op_worker

    pool = await _pool()
    try:
        await _clean(pool)
        acc_id = (await _fleet(pool, 1))[0]
        await op_worker._on_account_banned(pool, OWNER, acc_id, "invite")
        await op_worker._on_account_banned(pool, OWNER, acc_id, "bulk_op")
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM restriction_events WHERE account_id=$1", acc_id)
        assert n == 1, f"записей {n}: дедуп не сработал, таблица будет распухать"
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_the_operations_gate_sees_the_ban():
    """Гейт операций читает ту же таблицу — бан обязан уводить в карантин."""
    from services import infra_memory, op_worker

    pool = await _pool()
    try:
        await _clean(pool)
        acc_id = (await _fleet(pool, 1))[0]
        assert await infra_memory.is_account_quarantined(pool, acc_id) is False
        await op_worker._on_account_banned(pool, OWNER, acc_id, "invite")
        assert await infra_memory.is_account_quarantined(pool, acc_id) is True, (
            "аккаунт, который продукт сам признал забаненным, гейт операций "
            "считает годным: он не знал о собственных находках бана")
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_manual_risk_reset_still_lifts_the_quarantine():
    """Кнопка «взять в работу» обязана работать и на этом сигнале."""
    from services import account_reset, infra_memory, op_worker

    pool = await _pool()
    try:
        await _clean(pool)
        acc_id = (await _fleet(pool, 1))[0]
        await op_worker._on_account_banned(pool, OWNER, acc_id, "invite")
        assert await infra_memory.is_account_quarantined(pool, acc_id) is True
        assert await account_reset.reset_account(pool, acc_id, OWNER) is True
        assert await infra_memory.is_account_quarantined(pool, acc_id) is False, (
            "снятие риска владельцем не действует на новый сигнал — это та же "
            "жалоба «кулдаун не сбрасывается»")
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_a_wave_of_dead_sessions_does_not_sleep_the_fleet():
    """Свои же отозванные сессии — не чистка Telegram."""
    from services import anti_storm, op_worker

    pool = await _pool()
    try:
        await _clean(pool)
        ids = await _fleet(pool, 12)
        for acc_id in ids[:6]:
            await op_worker._on_account_banned(
                pool, OWNER, acc_id, "bulk_dm_adhoc",
                event_type="session_dead", severity="warning")
        st = await anti_storm.current(pool, OWNER)
        assert st["level"] == "calm", (
            f"флот усыплён из-за мёртвых сессий ({st}): перезалить сессии это "
            "не поможет, а операции встанут на полчаса")
    finally:
        await _clean(pool)
        await pool.close()
