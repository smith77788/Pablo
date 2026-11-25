"""Числа на вкладках и чужой владелец — на живой базе.

Четыре счётчика собираются четырьмя запросами с `FILTER`/`EXISTS` по трём
таблицам: заглушка пула подтвердила бы форму кода, а не то, что Postgres
примет запрос и отберёт правильные строки. Именно на этом классе продукт
ломался молча (CLAUDE.md: «проверка текста запроса не проверяет, что база его
примет»).

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/ci pytest …
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 991461, 991462
BOT_ID = 880099461
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    for sql in (
        "DELETE FROM notification_feed WHERE owner_id = ANY($1::bigint[])",
        "DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
        "DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
        "DELETE FROM user_proxies WHERE owner_id = ANY($1::bigint[])",
    ):
        await pool.execute(sql, [OWNER, STRANGER])
    await pool.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT_ID)
    await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


async def _seed(pool):
    await _clean(pool)
    dead_proxy = await pool.fetchval(
        "INSERT INTO user_proxies(owner_id, proxy_url, is_alive) "
        "VALUES($1,'socks5://dead:1080',FALSE) RETURNING id", OWNER)
    live_proxy = await pool.fetchval(
        "INSERT INTO user_proxies(owner_id, proxy_url, is_alive) "
        "VALUES($1,'socks5://live:1080',TRUE) RETURNING id", OWNER)
    rows = (
        (OWNER, "+79990000461", "banned", None),        # в беде
        (OWNER, "+79990000462", "spamblock", None),     # в беде
        (OWNER, "+79990000463", "ok", dead_proxy),      # простаивает из-за прокси
        (OWNER, "+79990000464", "ok", live_proxy),      # здоров
        (OWNER, "+79990000465", "ok", None),            # здоров
        (STRANGER, "+79990000466", "banned", None),     # чужая беда
    )
    for owner, phone, status, proxy in rows:
        await pool.execute(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, proxy_id) "
            "VALUES($1,$2,'sess',TRUE,$3,$4)", owner, phone, status, proxy)
    # Кулдаун — не беда: он проходит сам, значок о нём был бы ложной тревогой.
    await pool.execute(
        "UPDATE tg_accounts SET cooldown_until = now() + interval '1 hour' "
        "WHERE owner_id=$1 AND phone='+79990000465'", OWNER)

    await pool.execute(
        "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
        "VALUES($1,$2,'badges_test_bot',TRUE,$3)", f"1234:badges-{BOT_ID}", BOT_ID, OWNER)
    for status, owner in (("running", OWNER), ("pending", OWNER),
                          ("completed", OWNER), ("running", STRANGER)):
        await pool.execute(
            "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
            "VALUES($1,'залп',5,$2,$3)", BOT_ID, status, owner)

    from database import db
    await db._feed_record(pool, OWNER, "notify_ops", "операция завершилась", None)
    await db._feed_record(pool, OWNER, "notify_error", "аккаунт ограничен", None)
    await db._feed_record(pool, STRANGER, "notify_ops", "чужое", None)
    await pool.execute(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,'mass_check','running','{}'::jsonb,1,'значки')", OWNER)


@live
def test_the_badges_count_what_needs_attention_and_only_mine():
    from services import mini_app_badges

    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await _seed(pool)
            mine = await mini_app_badges.collect(pool, OWNER)
            stranger = await mini_app_badges.collect(pool, STRANGER)
            return mine, stranger
        finally:
            try:
                await _clean(pool)
            finally:
                await pool.close()

    mine, stranger = _run(go())
    assert mine["accounts"] == 3, (
        "в беде: заблокированный, спамблок и простаивающий из-за мёртвого прокси; "
        f"кулдаун бедой не считается, получено {mine}")
    assert mine["broadcasts"] == 2, f"идут pending и running, завершённая — нет: {mine}"
    assert mine["notifications"] == 2, mine
    assert mine["operations"] == 1, mine
    # Чужая беда и чужая рассылка не попадают в мои значки, и наоборот.
    assert stranger["accounts"] == 1 and stranger["broadcasts"] == 1
    assert stranger["notifications"] == 1 and stranger["operations"] == 0
    # Плашка «идёт операция» показывает мою работу и только мою.
    assert mine["op"] and mine["op"]["label"] == "значки", mine["op"]
    assert stranger["op"] is None
