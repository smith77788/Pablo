"""Восстановление подписок из истории платежей (CryptoBot + Telegram Stars).

Ядро — дата-привязанный расчёт срока (compute_expiries): продление от даты
ПЛАТЕЖА, а не от «сегодня». Плюс разбор payload, заборщики истории (фейки),
reconcile (сухой прогон и запись), разводка.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import payment_recovery as pr  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _p(uid, plan, months, days_ago, source="cryptopay", ref="x"):
    paid = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return pr.Payment(uid, plan, months, paid, source, ref)


# ── parse_payload ──────────────────────────────────────────────────────────────
def test_parse_payload_valid_and_invalid():
    assert pr.parse_payload("123:pro:6") == (123, "pro", 6)
    assert pr.parse_payload("123:pro:6:extra") == (123, "pro", 6)  # лишнее игнор
    assert pr.parse_payload("") is None
    assert pr.parse_payload("no-colons") is None
    assert pr.parse_payload("abc:pro:6") is None      # user_id не число
    assert pr.parse_payload("123::6") is None          # пустой план
    assert pr.parse_payload("0:pro:6") is None         # нулевой uid
    assert pr.parse_payload("123:pro:xx") is None      # months не число
    assert pr.parse_payload("123:pro:0") == (123, "pro", 1)  # months<1 → 1


# ── compute_expiries: дата-привязанная логика ──────────────────────────────────
def test_single_payment_expiry_from_payment_date():
    # платёж 100 дней назад за 6 мес (~180 дней) → ещё активен ~80 дней
    res = pr.compute_expiries([_p(1, "pro", 6, days_ago=100)])
    assert 1 in res
    exp = res[1]["expires_at"]
    assert exp > datetime.now(timezone.utc)
    # срок ≈ paid + 180 дней
    assert res[1]["plan"] == "pro" and res[1]["months_total"] == 6


def test_expired_payment_excluded():
    # платёж 400 дней назад за 6 мес → давно истёк → не в результате
    res = pr.compute_expiries([_p(2, "pro", 6, days_ago=400)])
    assert 2 not in res


def test_two_payments_while_active_extend_from_expiry():
    # два платежа по 6 мес: второй через 30 дней после первого (пока активен) →
    # суммарно ~12 мес от первой даты
    now = datetime.now(timezone.utc)
    p1 = pr.Payment(3, "pro", 6, now - timedelta(days=60), "cryptopay", "a")
    p2 = pr.Payment(3, "pro", 6, now - timedelta(days=30), "stars", "b")
    res = pr.compute_expiries([p2, p1])   # порядок на входе не важен
    assert res[3]["months_total"] == 12
    # срок ≈ первая дата + 360 дней
    expected = (now - timedelta(days=60)) + timedelta(days=360)
    assert abs((res[3]["expires_at"] - expected).total_seconds()) < 3600


def test_second_payment_after_expiry_restarts_from_its_date():
    now = datetime.now(timezone.utc)
    # первый платёж истёк (1 мес, 200 дней назад), второй — 10 дней назад на 3 мес
    p1 = pr.Payment(4, "pro", 1, now - timedelta(days=200), "cryptopay", "a")
    p2 = pr.Payment(4, "vip", 3, now - timedelta(days=10), "cryptopay", "b")
    res = pr.compute_expiries([p1, p2])
    assert res[4]["months_total"] == 3          # не суммируется с истёкшим
    assert res[4]["plan"] == "vip"              # план последнего платежа
    expected = (now - timedelta(days=10)) + timedelta(days=90)
    assert abs((res[4]["expires_at"] - expected).total_seconds()) < 3600


def test_multiple_users_independent():
    res = pr.compute_expiries([
        _p(10, "pro", 12, days_ago=30),
        _p(11, "vip", 1, days_ago=5),
        _p(12, "pro", 1, days_ago=100),   # истёк
    ])
    assert set(res) == {10, 11}


# ── заборщики истории (фейки) ──────────────────────────────────────────────────
def test_fetch_cryptopay_no_token_returns_empty():
    assert _run(pr.fetch_cryptopay(object(), "")) == []


def test_fetch_cryptopay_parses_paid_invoices():
    class _Resp:
        def __init__(self, payload):
            self._payload = payload
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def json(self):
            return self._payload

    class _Http:
        def __init__(self):
            self.calls = 0
        def get(self, url, headers=None, params=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return _Resp({"ok": True, "result": {"items": [
                    {"invoice_id": 1, "payload": "555:pro:6", "paid_at": "2026-05-01T00:00:00Z"},
                    {"invoice_id": 2, "payload": "мусор", "paid_at": "2026-05-01T00:00:00Z"},
                ]}})
            return _Resp({"ok": True, "result": {"items": []}})

    res = _run(pr.fetch_cryptopay(_Http(), "token"))
    assert len(res) == 1 and res[0].user_id == 555 and res[0].source == "cryptopay"


def test_fetch_stars_parses_transactions():
    class _Src:
        invoice_payload = "777:vip:3"
    class _Tx:
        id = "tx1"
        date = datetime(2026, 6, 1, tzinfo=timezone.utc)
        source = _Src()
    class _TxNoPayload:
        id = "tx2"
        date = datetime(2026, 6, 1, tzinfo=timezone.utc)
        source = None
    class _Res:
        transactions = [_Tx(), _TxNoPayload()]
    class _Bot:
        def __init__(self):
            self.n = 0
        async def get_star_transactions(self, offset=0, limit=100):
            self.n += 1
            return _Res() if self.n == 1 else type("E", (), {"transactions": []})()

    res = _run(pr.fetch_stars(_Bot()))
    assert len(res) == 1 and res[0].user_id == 777 and res[0].months == 3


def test_fetch_stars_failsoft_on_error():
    class _Bot:
        async def get_star_transactions(self, offset=0, limit=100):
            raise RuntimeError("нет прав getStarTransactions")
    assert _run(pr.fetch_stars(_Bot())) == []


# ── reconcile: сухой прогон и запись ───────────────────────────────────────────
class _FakePool:
    def __init__(self):
        self.execs = []
    def acquire(self):
        pool = self
        class _Ctx:
            async def __aenter__(self):
                return _Conn(pool)
            async def __aexit__(self, *a):
                return False
        return _Ctx()


class _Conn:
    def __init__(self, pool):
        self.pool = pool
    async def execute(self, sql, *args):
        self.pool.execs.append((sql, args))
        return "OK"


def test_reconcile_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(pr, "fetch_cryptopay",
                        lambda http, token: _acoro([_p(1, "pro", 12, 10)]))
    monkeypatch.setattr(pr, "fetch_stars", lambda bot: _acoro([]))
    pool = _FakePool()
    rep = _run(pr.reconcile(pool, object(), object(), dry_run=True))
    assert rep["dry_run"] is True and rep["active"] == 1 and rep["applied"] == 0
    assert pool.execs == [], "сухой прогон не должен писать в БД"
    assert rep["found"] == 1 and rep["sample"][0]["user_id"] == 1


def test_reconcile_apply_writes_subscriptions(monkeypatch):
    monkeypatch.setattr(pr, "fetch_cryptopay",
                        lambda http, token: _acoro([_p(1, "pro", 12, 10), _p(2, "vip", 6, 5)]))
    monkeypatch.setattr(pr, "fetch_stars", lambda bot: _acoro([]))
    pool = _FakePool()
    rep = _run(pr.reconcile(pool, object(), object(), dry_run=False))
    assert rep["applied"] == 2
    # на каждого — upsert подписки + апдейт platform_users
    subs = [e for e in pool.execs if "INSERT INTO subscriptions" in e[0]]
    assert len(subs) == 2


def _acoro(v):
    async def _c(*a, **k):
        return v
    return _c()


# ── разводка ───────────────────────────────────────────────────────────────────
def test_wired_command():
    a = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    assert 'Command("reconcile_payments")' in a


# ── живой Postgres: apply реально ставит срок от даты платежа ──────────────────
@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_reconcile_apply_on_real_schema(monkeypatch):
    import asyncpg

    async def _r():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions("
                "user_id BIGINT PRIMARY KEY, plan TEXT, expires_at TIMESTAMPTZ, "
                "is_active BOOLEAN, started_at TIMESTAMPTZ)")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS platform_users("
                "user_id BIGINT PRIMARY KEY, current_plan TEXT, plan_expires_at TIMESTAMPTZ)")
            await conn.execute("DELETE FROM subscriptions WHERE user_id=808080")
            await conn.execute("DELETE FROM platform_users WHERE user_id=808080")
            await conn.execute("INSERT INTO platform_users(user_id) VALUES(808080)")
        finally:
            await conn.close()

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            monkeypatch.setattr(
                pr, "fetch_cryptopay",
                lambda http, token: _acoro([_p(808080, "pro", 12, 30)]))
            monkeypatch.setattr(pr, "fetch_stars", lambda bot: _acoro([]))
            rep = await pr.reconcile(pool, object(), object(), dry_run=False)
            assert rep["applied"] == 1
            async with pool.acquire() as c:
                row = await c.fetchrow(
                    "SELECT plan, expires_at, is_active FROM subscriptions WHERE user_id=808080")
                assert row["plan"] == "pro" and row["is_active"] is True
                # срок ≈ (30 дней назад) + 360 дней → примерно через 330 дней
                assert row["expires_at"] > datetime.now(timezone.utc) + timedelta(days=300)
                pu = await c.fetchrow(
                    "SELECT current_plan FROM platform_users WHERE user_id=808080")
                assert pu["current_plan"] == "pro"
        finally:
            await pool.close()
            conn = await asyncpg.connect(DSN)
            try:
                await conn.execute("DELETE FROM subscriptions WHERE user_id=808080")
                await conn.execute("DELETE FROM platform_users WHERE user_id=808080")
            finally:
                await conn.close()

    _run(_r())
