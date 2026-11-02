"""Три тарифа по объёму флота: Старт 15 $ (10 аккаунтов), Про 29 $ (50), Бизнес 79 $.

РАЗРЫВ. Подписка была одна — 29 $ за всё без ограничений: владельцу с тремя
аккаунтами дорого, агентству с сотней — дёшево. Решение владельца: возможности
одинаковые, различается размер флота. Тариф выбирается при оплате, едет в
счёте (payments.tier) до подписки (subscriptions.tier), а лимит держит одна
дверь записи аккаунта — db.upsert_tg_account, на всех входах сразу.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from bot.utils import tariffs
from services import tiers


def test_defaults_match_the_owner_decision(monkeypatch):
    for k in ("PRICE_TIER_START", "PRICE_TIER_PRO", "PRICE_TIER_BUSINESS",
              "LIMIT_TIER_START_ACCOUNTS", "LIMIT_TIER_PRO_ACCOUNTS",
              "LIMIT_TIER_BUSINESS_ACCOUNTS"):
        monkeypatch.delenv(k, raising=False)
    assert [tiers.price_usd(t) for t in tiers.TIERS] == [15, 29, 79]
    assert [tiers.account_cap(t) for t in tiers.TIERS] == [10, 50, tariffs.UNLIMITED]
    assert tiers.account_cap(None) == tariffs.UNLIMITED, (
        "подписка до тарифов и триал сохраняют прежние условия — без лимита")
    assert [tiers.for_fleet(n) for n in (0, 10, 11, 50, 51)] == \
        ["start", "start", "pro", "pro", "business"]
    monkeypatch.setenv("PRICE_TIER_START", "19")
    monkeypatch.setenv("LIMIT_TIER_START_ACCOUNTS", "unlimited")
    assert tiers.price_usd("start") == 19
    assert tiers.account_cap("start") == tariffs.UNLIMITED


def test_price_is_the_tier_price_and_stale_buttons_stay_at_29():
    from bot.handlers import subscription as sh

    assert sh._calc("paid", 1, "USDT_TRC20", tier="start") == (15, 15)
    assert sh._calc("paid", 12, "USDT_TRC20", tier="business")[0] == round(79 * 12 * 0.8, 2)
    assert sh._calc("paid", 1, "USDT_TRC20")[0] == 29, "счёт без тарифа — прежняя цена"
    assert tiers.DEFAULT_TIER == "pro" and tiers.price_usd("pro") == 29


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 99_000_931


async def _setup(pool):
    from services import trial  # noqa: F401  (таблица subscription_trials — схема)

    await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", OWNER)
    await pool.execute("DELETE FROM subscription_trials WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM payments WHERE user_id=$1", OWNER)


def _live(fn):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=2, max_size=8)
        try:
            await _setup(pool)
            return await fn(pool)
        finally:
            await _setup(pool)
            await pool.close()
    return asyncio.new_event_loop().run_until_complete(go())


def _add(pool, n):
    from database import db
    return db.upsert_tg_account(pool, OWNER, "", f"sess-tier-{n}", 7_700_000 + n, "И", "u")


def test_tier_cap_is_held_at_the_door_for_new_accounts_only(monkeypatch):
    monkeypatch.setenv("LIMIT_TIER_START_ACCOUNTS", "2")
    from bot.utils import subscription as sub
    from database import db
    from services import billing

    async def go(pool):
        async with pool.acquire() as conn:
            await billing.activate_subscription(conn, OWNER, "paid", 1, "start")
        sub.invalidate_plan_cache(OWNER)
        await _add(pool, 1)
        await _add(pool, 2)
        with pytest.raises(db.AccountLimitReached) as e:
            await _add(pool, 3)
        _, created, _ = await _add(pool, 1)  # переимпорт уже подключённого
        tier = await pool.fetchval("SELECT tier FROM subscriptions WHERE user_id=$1", OWNER)
        count = await pool.fetchval("SELECT count(*) FROM tg_accounts WHERE owner_id=$1", OWNER)
        # Апгрейд до Про — лимит снимается сразу.
        async with pool.acquire() as conn:
            await billing.activate_subscription(conn, OWNER, "paid", 1, "pro")
        await _add(pool, 3)
        return str(e.value), created, tier, count

    msg, created, tier, count = _live(go)
    assert "Лимит тарифа — 2 аккаунтов" in msg and "/subscription" in msg
    assert created is False, "переимпорт не должен упираться в лимит"
    assert (tier, count) == ("start", 2)


def test_parallel_import_cannot_jump_over_the_cap(monkeypatch):
    monkeypatch.setenv("LIMIT_TIER_START_ACCOUNTS", "3")
    from bot.utils import subscription as sub
    from database import db
    from services import billing

    async def go(pool):
        await billing.activate_subscription(pool, OWNER, "paid", 1, "start")
        sub.invalidate_plan_cache(OWNER)
        res = await asyncio.gather(*[_add(pool, n) for n in range(8)], return_exceptions=True)
        refused = [r for r in res if isinstance(r, db.AccountLimitReached)]
        other = [r for r in res if isinstance(r, Exception) and r not in refused]
        count = await pool.fetchval("SELECT count(*) FROM tg_accounts WHERE owner_id=$1", OWNER)
        return count, len(refused), other

    count, refused, other = _live(go)
    assert not other, other
    assert (count, refused) == (3, 5), "пакетный импорт проскочил лимит тарифа"


def test_free_owner_gets_one_account_then_the_trial(monkeypatch):
    monkeypatch.delenv("TRIAL_DAYS", raising=False)
    from bot.utils import subscription as sub
    from database import db

    async def go(pool):
        sub.invalidate_plan_cache(OWNER)
        await _add(pool, 1)
        await _add(pool, 2)  # упирается в бесплатный лимит → выдаётся триал
        trial = await pool.fetchval(
            "SELECT count(*) FROM subscription_trials WHERE owner_id=$1", OWNER)
        # Триал кончился, подписки нет — новый аккаунт только по подписке.
        await pool.execute("UPDATE subscriptions SET expires_at=now() - INTERVAL '1 day' "
                           "WHERE user_id=$1", OWNER)
        sub.invalidate_plan_cache(OWNER)
        with pytest.raises(db.AccountLimitReached) as e:
            await _add(pool, 3)
        return trial, str(e.value)

    trial, msg = _live(go)
    assert trial == 1, "второй аккаунт новичка не открыл пробный период"
    assert "по подписке" in msg


def test_payment_carries_the_tier_to_the_subscription():
    from bot.handlers import subscription as sh

    async def go(pool):
        await pool.execute(
            """INSERT INTO payments (user_id, plan, period_months, currency, amount_crypto,
                   amount_usd, wallet_address, reference, expires_at, tier)
               VALUES ($1,'paid',1,'XTR',1500,15,'telegram_stars','PAY-TIER01',
                       now() + INTERVAL '1 hour', 'start')""", OWNER)
        res = await sh.confirm_stars_payment(pool, OWNER, f"{OWNER}:paid:1:PAY-TIER01",
                                             "charge-tier", 1500)
        tier = await pool.fetchval("SELECT tier FROM subscriptions WHERE user_id=$1", OWNER)
        return res, tier

    res, tier = _live(go)
    assert res is not None and tier == "start"
