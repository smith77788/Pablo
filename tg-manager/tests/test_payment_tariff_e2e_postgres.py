"""Оплата и гейт прогрева — на НАСТОЯЩЕМ Postgres.

ЗАЧЕМ ОТДЕЛЬНО. Заглушка пула не исполняет SQL: advisory-блокировка, выборка
открытых счетов, `make_interval`, INSERT…SELECT с `ON CONFLICT … WHERE` и
проверка «хэш уже подтвердил другой счёт» на ней не проверяются вовсе. Ошибка
в любом из этих запросов ломает приём оплаты целиком — с зелёными юнит-тестами.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import datetime
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990581
STRANGER = 990582
WALLET = "TTESTWALLET0000000000000000000000"
_LOOP: "asyncio.AbstractEventLoop | None" = None


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
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


async def _wipe(p) -> None:
    await p.execute("DELETE FROM payments WHERE user_id = ANY($1::bigint[])",
                    [OWNER, STRANGER])
    await p.execute("DELETE FROM account_warmup_plans WHERE owner_id = ANY($1::bigint[])",
                    [OWNER, STRANGER])
    await p.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                    [OWNER, STRANGER])
    await p.execute("DELETE FROM subscriptions WHERE user_id = ANY($1::bigint[])",
                    [OWNER, STRANGER])


def test_open_invoices_never_share_an_amount(pool):
    from services import usdt_invoice

    async def go():
        await _wipe(pool)
        amounts = await asyncio.gather(*[
            usdt_invoice.create_payment(
                pool, user_id=OWNER, plan="paid", period_months=1, base_usdt=29,
                amount_usd=29, wallet=WALLET, reference=f"PAY-E2E{i:03d}")
            for i in range(20)
        ])
        rows = await pool.fetch(
            "SELECT amount_crypto, expires_at > now() + INTERVAL '23 hours' AS ttl_ok "
            "FROM payments WHERE user_id=$1", OWNER)
        return amounts, rows

    amounts, rows = _run(go())
    assert len(set(amounts)) == 20, "одновременные счета получили одну сумму"
    assert all(29.01 <= a <= 29.99 for a in amounts)
    assert len(rows) == 20 and all(r["ttl_ok"] for r in rows)


def test_checker_selects_open_invoices_and_refuses_reused_tx(pool, monkeypatch):
    from services import payment_checker as pc

    granted: list = []

    async def _activate(executor, user_id, plan, months):
        granted.append((user_id, months))

    monkeypatch.setattr(pc, "_activate_subscription", _activate)

    async def go():
        await _wipe(pool)
        for ref in ("PAY-E2EA", "PAY-E2EB"):
            await pool.execute(
                """INSERT INTO payments (user_id, plan, period_months, currency,
                                         amount_crypto, amount_usd, wallet_address,
                                         reference, expires_at)
                   VALUES ($1,'paid',1,'USDT_TRC20',29.11,29,$2,$3,
                           now() + INTERVAL '1 hour')""",
                OWNER, WALLET, ref)
        a = await pool.fetchrow("SELECT * FROM payments WHERE reference='PAY-E2EA'")
        b = await pool.fetchrow("SELECT * FROM payments WHERE reference='PAY-E2EB'")
        await pc._confirm(pool, None, a, "TXE2E1")
        await pc._confirm(pool, None, b, "TXE2E1")  # тот же хэш — второму нельзя
        return await pool.fetch(
            "SELECT reference, status FROM payments WHERE user_id=$1 ORDER BY reference",
            OWNER)

    rows = _run(go())
    assert [(r["reference"], r["status"]) for r in rows] == [
        ("PAY-E2EA", "confirmed"), ("PAY-E2EB", "pending")]
    assert granted == [(OWNER, 1)]


def test_warmup_plan_is_scoped_to_its_owner(pool, monkeypatch):
    import bot.utils.subscription as sub
    from services import account_warmer as aw

    async def _yes(pool_, uid, min_plan):
        return True

    monkeypatch.setattr(sub, "require_plan", _yes)

    async def go():
        await _wipe(pool)
        acc = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES($1, '+79930000581', 's', true) RETURNING id", OWNER)
        plan_id = await aw.create_warmup_plan(pool, OWNER, acc, "gentle")
        await pool.execute(
            "UPDATE account_warmup_plans SET current_day=5 WHERE id=$1", plan_id)
        refused = False
        try:
            await aw.create_warmup_plan(pool, STRANGER, acc, "aggressive")
        except PermissionError:
            refused = True
        row = await pool.fetchrow(
            "SELECT owner_id, current_day, plan_type FROM account_warmup_plans "
            "WHERE account_id=$1", acc)
        return refused, row

    refused, row = _run(go())
    assert refused, "чужой владелец завёл план на этот аккаунт"
    assert (row["owner_id"], row["current_day"], row["plan_type"]) == (OWNER, 5, "gentle")


def test_subscription_notices_queries_run(pool, monkeypatch):
    import database.db as db
    import bot.utils.subscription as sub
    from services import subscription_notices as sn

    sent: list = []

    class _Bot:
        async def send_message(self, uid, text, **kw):
            sent.append((uid, text))

    async def _dedup(p, uid, key, cooldown):
        return True

    monkeypatch.setattr(db, "notify_dedup_ok", _dedup)
    monkeypatch.setattr(sub, "is_platform_admin", lambda uid: False)

    async def go():
        await _wipe(pool)
        now = datetime.datetime.now(datetime.timezone.utc)
        await pool.execute(
            "INSERT INTO subscriptions(user_id, plan, expires_at, is_active) "
            "VALUES($1,'paid',$2,true), ($3,'paid',$4,false)",
            OWNER, now + datetime.timedelta(days=2),
            STRANGER, now - datetime.timedelta(hours=3))
        return await sn.run_once(pool, _Bot())

    res = _run(go())
    mine = {uid for uid, _ in sent if uid in (OWNER, STRANGER)}
    assert mine == {OWNER, STRANGER}
    assert res["expiring"] >= 1 and res["expired"] >= 1
