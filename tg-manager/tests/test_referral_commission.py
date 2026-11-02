"""Реферер получает долю с КАЖДОЙ оплаты приглашённого.

РАЗРЫВ. Награды были пороговыми и разовыми, а `mark_referral_paid` отдаёт
реферера только при первой оплате приглашённого: продления не приносили
пригласившему ничего. Теперь services.referral_commission в транзакции
подтверждения платежа добавляет рефереру REFERRAL_COMMISSION_PCT (20 %) от
оплаченного срока днями подписки — один раз на платёж.
"""
from __future__ import annotations

import asyncio
import inspect
import os

import pytest

from services import referral_commission as rc


def test_commission_days(monkeypatch):
    monkeypatch.delenv("REFERRAL_COMMISSION_PCT", raising=False)
    assert rc.commission_pct() == 20
    assert rc.commission_days(1) == 6
    assert rc.commission_days(12) == 72
    assert rc.commission_days(0) == 0
    monkeypatch.setenv("REFERRAL_COMMISSION_PCT", "0")
    assert rc.commission_days(3) == 0, "0 — начисление выключено"
    monkeypatch.setenv("REFERRAL_COMMISSION_PCT", "90")
    assert rc.commission_pct() == 50, "потолок доли"


def test_both_payment_doors_credit_inside_the_confirmation():
    from bot.handlers import subscription
    from services import payment_checker

    assert "referral_commission.credit_in_tx(" in inspect.getsource(
        payment_checker._confirm), "крипто-оплата не начисляет долю рефереру"
    assert "referral_commission.credit_in_tx(" in inspect.getsource(
        subscription.confirm_stars_payment), "оплата Stars не начисляет долю рефереру"


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
REFERRER, REFERRED = 99_000_881, 99_000_882


def test_every_payment_credits_once_on_real_postgres(monkeypatch):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    monkeypatch.delenv("REFERRAL_COMMISSION_PCT", raising=False)
    ids = [REFERRER, REFERRED]

    async def cleanup(pool):
        await pool.execute("DELETE FROM referral_commissions WHERE referrer_id=$1", REFERRER)
        await pool.execute("DELETE FROM platform_referrals WHERE referred_id=$1", REFERRED)
        await pool.execute("DELETE FROM subscriptions WHERE user_id = ANY($1::bigint[])", ids)
        await pool.execute("DELETE FROM platform_users WHERE user_id = ANY($1::bigint[])", ids)

    async def credit(pool, ref, months):
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await rc.credit_in_tx(conn, REFERRED, ref, months)

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await cleanup(pool)
            for uid in ids:
                await pool.execute(
                    "INSERT INTO platform_users(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
            nobody = await credit(pool, "PAY-NOREF1", 1)
            await pool.execute(
                "INSERT INTO platform_referrals(referrer_id, referred_id, paid_at) "
                "VALUES($1, $2, now())", REFERRER, REFERRED)
            first = await credit(pool, "PAY-REFC01", 1)
            exp1 = await pool.fetchval(
                "SELECT expires_at FROM subscriptions WHERE user_id=$1", REFERRER)
            again = await credit(pool, "PAY-REFC01", 1)
            renewal = await credit(pool, "PAY-REFC02", 12)
            exp2 = await pool.fetchval(
                "SELECT expires_at FROM subscriptions WHERE user_id=$1", REFERRER)
            totals = await rc.totals(pool, REFERRER)
            return nobody, first, again, renewal, exp1, exp2, totals
        finally:
            await cleanup(pool)
            await pool.close()

    nobody, first, again, renewal, exp1, exp2, totals = \
        asyncio.new_event_loop().run_until_complete(go())
    assert nobody is None, "без реферера начислять некому"
    assert first == (REFERRER, 6) and exp1 is not None
    assert again is None, "повтор того же платежа начислил второй раз"
    assert renewal == (REFERRER, 72), "продление приглашённого не принесло доли"
    assert (exp2 - exp1).days == 72
    assert totals == (2, 78)
