"""Квота операций бесплатного тарифа и триал 7 дней.

РАЗРЫВ. Доступ к операциям был бинарным: после трёх дней триала владелец без
подписки не мог поставить ни одной операции — продукт выключался целиком. У
лидеров бесплатный уровень полезен сам по себе и упирается в объём. Теперь
operation_bus.submit списывает операцию из месячной квоты (services/free_quota),
а когда квота кончилась, отказ говорит именно это и ведёт к оплате.
"""
from __future__ import annotations

import asyncio
import datetime
import os

import pytest

import bot.utils.subscription as sub
from services import free_quota, operation_bus, trial

NOV = datetime.datetime(2026, 11, 15, tzinfo=datetime.UTC)


def _paid_op():
    for op, meta in operation_bus.OP_REGISTRY.items():
        if op not in free_quota.EXCLUDED_OPS and meta.get("min_plan") \
                and sub.coerce_plan(meta["min_plan"]) == "paid":
            return op
    pytest.skip("нет платной операции в OP_REGISTRY")


def test_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv("QUOTA_FREE_OPS_MONTH", raising=False)
    monkeypatch.delenv("TRIAL_DAYS", raising=False)
    assert free_quota.monthly_limit() == 10
    assert trial.trial_days() == 7, "трёх дней не хватало до первого результата"
    monkeypatch.setenv("QUOTA_FREE_OPS_MONTH", "0")
    assert not free_quota.covers("dm_campaign"), "0 — квота выключена"
    monkeypatch.setenv("QUOTA_FREE_OPS_MONTH", "abc")
    assert free_quota.monthly_limit() == 10
    assert not free_quota.covers("account_warmup"), (
        "прогрев проверяет тариф перед каждым днём: квота на постановку дала бы "
        "план, который встаёт на второй день")
    assert free_quota.month_start(NOV) == datetime.date(2026, 11, 1)


class _Pool:
    """Фейк для submit: квота (fetchval INSERT…RETURNING), постановка (fetchrow)."""

    def __init__(self, *, quota_left=True, reuse=False, insert_fails=False):
        self.quota_left = quota_left
        self.reuse = reuse
        self.insert_fails = insert_fails
        self.consumed = 0
        self.refunded = 0
        self.inserted = False

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _A()

    def transaction(self):
        class _T:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *e):
                return False
        return _T()

    async def fetchval(self, sql, *args):
        if "free_op_usage" in sql:
            if not self.quota_left:
                return None
            self.consumed += 1
            return self.consumed
        return 555 if self.reuse else None

    async def execute(self, sql, *args):
        if "free_op_usage" in sql:
            self.refunded += 1
        return "OK"

    async def fetchrow(self, sql, *args):
        if self.insert_fails:
            raise RuntimeError("db down")
        self.inserted = True
        return {"id": 777}


@pytest.fixture
def free_owner(monkeypatch):
    async def _no(pool, owner_id, plan):
        return False

    async def _no_trial(pool, owner_id):
        return None

    async def _quiet(*a, **k):
        return None

    monkeypatch.setattr(sub, "require_plan", _no)
    monkeypatch.setattr(trial, "maybe_grant", _no_trial)
    monkeypatch.setattr(operation_bus, "_enforce_immunity", _quiet)
    monkeypatch.delenv("QUOTA_FREE_OPS_MONTH", raising=False)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_free_owner_can_queue_a_paid_operation_from_the_quota(free_owner):
    pool = _Pool()
    op_id = _run(operation_bus.submit(pool, 42, _paid_op(), {"x": 1}))
    assert op_id == 777 and pool.inserted and pool.consumed == 1, (
        "владелец без подписки снова упирается в стену вместо квоты")


def test_spent_quota_says_so_and_leads_to_payment(free_owner):
    pool = _Pool(quota_left=False)
    with pytest.raises(operation_bus.PlanRequiredError) as e:
        _run(operation_bus.submit(pool, 42, _paid_op(), {"x": 1}))
    assert e.value.quota_spent
    assert "Бесплатные 10 операций в этом месяце закончились" in str(e.value)
    assert "по подписке" in str(e.value)
    assert not pool.inserted


def test_warmup_is_not_bought_with_the_quota(free_owner):
    if "account_warmup" not in operation_bus.OP_REGISTRY:
        pytest.skip("нет прогрева в реестре")
    meta = operation_bus.OP_REGISTRY["account_warmup"]
    if not meta.get("min_plan") or sub.coerce_plan(meta["min_plan"]) == "free":
        pytest.skip("прогрев бесплатный")
    pool = _Pool()
    with pytest.raises(operation_bus.PlanRequiredError) as e:
        _run(operation_bus.submit(pool, 42, "account_warmup", {"x": 1}))
    assert not e.value.quota_spent and pool.consumed == 0


def test_unit_comes_back_when_the_operation_does_not_queue(free_owner, monkeypatch):
    op = _paid_op()

    async def _blocked(pool, owner_id, op_type):
        raise operation_bus.ImmunityBlockedError(op_type, "волна банов", None)

    monkeypatch.setattr(operation_bus, "_enforce_immunity", _blocked)
    pool = _Pool()
    with pytest.raises(operation_bus.ImmunityBlockedError):
        _run(operation_bus.submit(pool, 42, op, {"x": 1}))
    assert pool.refunded == 1, "Ban Weather отказал, а квота сгорела"

    monkeypatch.setattr(operation_bus, "_enforce_immunity", lambda *a: _none())
    pool = _Pool(insert_fails=True)
    with pytest.raises(RuntimeError):
        _run(operation_bus.submit(pool, 42, op, {"x": 1}))
    assert pool.refunded == 1, "постановка упала, а квота сгорела"

    pool = _Pool(reuse=True)
    assert _run(operation_bus.submit(pool, 42, op, {"x": 1})) == 555
    assert pool.refunded == 1, "двойной тап съел две единицы квоты"


async def _none():
    return None


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_consume_is_atomic_and_monthly_on_real_postgres(monkeypatch):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    monkeypatch.setenv("QUOTA_FREE_OPS_MONTH", "3")
    owner = 99_000_771

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=2, max_size=6)
        try:
            await pool.execute("DELETE FROM free_op_usage WHERE owner_id=$1", owner)
            got = await asyncio.gather(*[
                free_quota.consume(pool, owner, "dm_campaign", now=NOV) for _ in range(6)])
            left = await free_quota.remaining(pool, owner, now=NOV)
            await free_quota.refund(pool, owner, now=NOV)
            after_refund = await free_quota.remaining(pool, owner, now=NOV)
            dec = await free_quota.consume(
                pool, owner, "dm_campaign", now=NOV.replace(month=12))
            return got, left, after_refund, dec
        finally:
            await pool.execute("DELETE FROM free_op_usage WHERE owner_id=$1", owner)
            await pool.close()

    got, left, after_refund, dec = asyncio.new_event_loop().run_until_complete(go())
    assert sum(got) == 3, f"шесть одновременных постановок взяли {sum(got)} из 3"
    assert left == 0 and after_refund == 1
    assert dec is True, "новый месяц — новая квота"
