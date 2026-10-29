"""Дыры модуля оплаты и тарифов (D1, D2, D3, D5, D18 из OPEN_DEFECTS.md).

D1. Подделанная кнопка `months=0` давала цену 0, а activate_subscription
    делал max(1, months) и выдавал месяц. Срок теперь проверяется на сервере
    (`valid_period`), счёт на ноль не создаётся и не подтверждается, а billing
    отказывает в сроке меньше месяца.
D2. USDT (TRC-20) сверялся только по сумме ±0,02, и перевод одного человека
    подтверждал счёт другого. Теперь у каждого открытого счёта своя сумма до
    цента (`usdt_invoice`), сверка точная, перевод не старше счёта, а одна
    транзакция подтверждает ровно один счёт.
D3. Тариф прогрева проверял только submit(): массовый прогрев, бот и цикл
    грели без подписки. Теперь единый гейт `operation_bus.enforce_gates`
    стоит при создании плана и перед каждым днём прогрева.
D5. План прогрева в боте писался на чужой аккаунт по подделанному callback.
D18. О конце подписки владельцу не сообщалось.
"""
from __future__ import annotations

import ast
import datetime
import pathlib

import pytest

from bot.callbacks import SubCb

ROOT = pathlib.Path(__file__).resolve().parents[1]
UTC = datetime.timezone.utc


def _now():
    return datetime.datetime.now(UTC)


def _func_src(rel: str, name: str) -> str:
    src = (ROOT / rel).read_text("utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} не найдена в {rel}")


# ── D1: срок подписки проверяется на сервере ────────────────────────────────

def test_only_sold_periods_are_valid():
    from bot.handlers.subscription import valid_period, ALLOWED_PERIODS

    assert ALLOWED_PERIODS == (1, 3, 6, 12)
    for bad in (0, -1, 2, 24, "x", None, "0"):
        assert valid_period(bad) is None, bad
    for ok in (1, 3, 6, 12, "3"):
        assert valid_period(ok) == int(ok)


def test_price_for_zero_months_is_refused_not_zero():
    from bot.handlers.subscription import _calc

    with pytest.raises(ValueError):
        _calc("paid", 0, "USDT_TRC20")
    usd, crypto = _calc("paid", 3, "USDT_TRC20")
    assert usd > 0 and crypto > 0


class _Msg:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)


class _User:
    id = 555


class _Callback:
    def __init__(self, data: str = "sub:pay"):
        self.from_user = _User()
        self.data = data
        self.message = _Msg()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class _RecPool:
    """Пул, который запоминает запросы и никогда не должен получить INSERT."""

    def __init__(self):
        self.queries: list[str] = []

    async def fetchrow(self, q, *a):
        self.queries.append(q)
        return None

    async def execute(self, q, *a):
        self.queries.append(q)
        return "INSERT 0 1"

    async def fetch(self, q, *a):
        self.queries.append(q)
        return []


@pytest.mark.parametrize("months", [0, -3, 2])
async def test_forged_period_button_creates_no_invoice(monkeypatch, months):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_tron_wallet", lambda: "TWALLET123456")
    monkeypatch.setattr(sh, "_ton_wallet", lambda: "UQTONWALLET123")
    cb = _Callback()
    pool = _RecPool()
    data = SubCb(action="pay", plan="paid", months=months, currency="USDT_TRC20")
    await sh.cb_pay(cb, data, pool)
    assert not any("INSERT INTO payments" in q for q in pool.queries), (
        "по подделанному сроку создан счёт")
    assert cb.answers and cb.answers[0][1] is True, "отказ должен быть виден человеку"


async def test_unknown_currency_creates_no_invoice(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_tron_wallet", lambda: "TWALLET123456")
    cb = _Callback()
    pool = _RecPool()
    await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency="BTC"), pool)
    assert not any("INSERT INTO payments" in q for q in pool.queries)


async def test_forged_period_in_admin_grant_and_request_is_refused(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh.sub_utils, "is_platform_admin", lambda uid: True)
    cb = _Callback()
    pool = _RecPool()
    await sh.cb_admin_grant(cb, SubCb(action="admin_grant", plan="paid", months=0), pool)
    assert not any("subscriptions" in q for q in pool.queries)
    cb2 = _Callback()
    await sh.cb_request_sub(cb2, SubCb(action="request_sub", plan="paid", months=0))
    assert cb2.answers and cb2.answers[0][1] is True
    assert not cb2.message.edits


async def test_billing_refuses_less_than_a_month():
    from services import billing

    class _Exec:
        async def fetchval(self, *a):
            raise AssertionError("подписка не должна выдаваться")

        async def execute(self, *a):
            raise AssertionError("подписка не должна выдаваться")

    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            await billing.activate_subscription(_Exec(), 1, "paid", bad)


def _pay(**kw):
    base = {
        "id": 1, "user_id": 10, "plan": "paid", "period_months": 1,
        "currency": "USDT_TRC20", "amount_crypto": 29.37, "reference": "PAY-A",
        "wallet_address": "TWALLET", "created_at": _now() - datetime.timedelta(minutes=10),
    }
    base.update(kw)
    return base


def test_checker_never_confirms_zero_or_forged_invoices():
    from services.payment_checker import _payable

    assert _payable(_pay())
    assert not _payable(_pay(period_months=0)), "счёт за 0 месяцев подтверждается"
    assert not _payable(_pay(period_months=2))
    assert not _payable(_pay(amount_crypto=0))
    assert _payable(_pay(plan="strike", period_months=0))
    assert _payable(_pay(plan="host_server", period_months=0))
    assert not _payable(_pay(plan="weird"))


# ── D2: перевод USDT подтверждает ровно один счёт ───────────────────────────

def _tx(txid="T1", quant=29_370_000, ts=None, **kw):
    t = {
        "transaction_id": txid, "quant": str(quant), "to_address": "TWALLET",
        "block_ts": int((ts or _now()).timestamp() * 1000), "confirmed": True,
        "contractRet": "SUCCESS", "finalResult": "SUCCESS",
        "tokenInfo": {"tokenId": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "tokenDecimal": 6},
    }
    t.update(kw)
    return t


def test_neighbouring_cent_does_not_confirm_someone_elses_invoice():
    """Раньше допуск ±0,02 отдавал перевод 29,37 первому счёту из 29,36/29,37/29,38."""
    from services.payment_checker import match_trc20

    a = _pay(id=1, amount_crypto=29.36, reference="PAY-A")
    b = _pay(id=2, amount_crypto=29.37, reference="PAY-B")
    c = _pay(id=3, amount_crypto=29.38, reference="PAY-C")
    matches, amb = match_trc20([a, b, c], [_tx(quant=29_370_000)], "TWALLET", set())
    assert [(p["reference"], t) for p, t in matches] == [("PAY-B", "T1")]
    assert not amb


def test_transfer_older_than_invoice_does_not_confirm_it():
    from services.payment_checker import match_trc20

    p = _pay(created_at=_now())
    old = _tx(ts=_now() - datetime.timedelta(hours=3))
    assert match_trc20([p], [old], "TWALLET", set()) == ([], [])


def test_ambiguous_transfer_confirms_nobody():
    """Два старых счёта с одной суммой — угадывать, чей перевод, нельзя."""
    from services.payment_checker import match_trc20

    a = _pay(id=1, reference="PAY-A")
    b = _pay(id=2, reference="PAY-B")
    matches, amb = match_trc20([a, b], [_tx()], "TWALLET", set())
    assert matches == []
    assert amb == [("T1", ["PAY-A", "PAY-B"])]


def test_unfinal_foreign_and_reused_transfers_are_ignored():
    from services.payment_checker import match_trc20

    p = _pay()
    for tx in (
        _tx(confirmed=False),
        _tx(contractRet="REVERT"),
        _tx(to_address="TOTHER"),
        _tx(tokenInfo={"tokenId": "TFAKEUSDT", "tokenDecimal": 6}),
    ):
        assert match_trc20([p], [tx], "TWALLET", set()) == ([], []), tx
    assert match_trc20([p], [_tx()], "TWALLET", {"T1"}) == ([], [])
    # Счёт, выставленный на прежний кошелёк, переводом на новый не подтверждается.
    assert match_trc20([_pay(wallet_address="TOLD")], [_tx()], "TWALLET", set()) == ([], [])


def test_one_transfer_confirms_one_invoice_and_one_invoice_takes_one_transfer():
    from services.payment_checker import match_trc20

    p = _pay()
    matches, _ = match_trc20([p], [_tx("T1"), _tx("T2")], "TWALLET", set())
    assert [t for _, t in matches] == ["T1"]


def test_wrong_amount_transfer_is_surfaced_not_swallowed():
    """Человек перевёл без центов — раньше об этом не узнавал никто."""
    from services.payment_checker import unmatched_trc20

    p = _pay(amount_crypto=29.37, created_at=_now() - datetime.timedelta(minutes=30))
    wrong = _tx("TW", quant=29_000_000)
    right = _tx("TR", quant=29_370_000)
    ancient = _tx("TA", quant=5_000_000, ts=_now() - datetime.timedelta(days=3))
    got = unmatched_trc20([p], [wrong, right, ancient], "TWALLET", set())
    assert got == [("TW", 29.0)]
    assert unmatched_trc20([p], [wrong], "TWALLET", {"TW"}) == []


def test_amount_is_unique_among_open_invoices():
    from services import usdt_invoice

    taken = {2900 + k for k in range(1, 99)}  # свободна только 29,99
    assert usdt_invoice.pick_amount(29, taken) == 29.99
    taken.add(2999)
    with pytest.raises(usdt_invoice.NoFreeAmountError):
        usdt_invoice.pick_amount(29, taken)


class _InvoiceConn:
    def __init__(self, open_amounts):
        self.open_amounts = open_amounts
        self.inserted: list = []
        self.locked = False

    def acquire(self):
        conn = self

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    def transaction(self):
        return self.acquire()

    async def execute(self, q, *a):
        if "pg_advisory_xact_lock" in q:
            self.locked = True
            return "SELECT 1"
        if "INSERT INTO payments" in q:
            assert self.locked, "сумма выделяется без блокировки"
            self.inserted.append(a)
        return "INSERT 0 1"

    async def fetch(self, q, *a):
        assert "wallet_address" in q and "confirmed_at IS NULL" in q
        return [{"amount_crypto": x} for x in self.open_amounts]


async def test_create_payment_never_reuses_an_open_amount():
    from services import usdt_invoice

    open_amounts = [29 + k / 100 for k in range(1, 99) if k != 42]
    conn = _InvoiceConn(open_amounts)
    amount = await usdt_invoice.create_payment(
        conn, user_id=1, plan="paid", period_months=1, base_usdt=29,
        amount_usd=29, wallet="TWALLET", reference="PAY-X")
    assert amount in (29.42, 29.99)
    assert conn.inserted and conn.inserted[0][3] == amount


class _ConfirmConn:
    """Транзакция _confirm: этот хэш уже подтвердил другой счёт."""

    def __init__(self, other_ref):
        self.other_ref = other_ref
        self.sql: list[str] = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Tx()

    async def execute(self, q, *a):
        self.sql.append(q)
        return "OK"

    async def fetchval(self, q, *a):
        self.sql.append(q)
        if "tx_hash=$1 AND id<>$2" in q:
            return self.other_ref
        if "UPDATE payments" in q:
            return 1
        return None


class _ConfirmPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def test_one_chain_transaction_confirms_only_one_invoice(monkeypatch):
    from services import payment_checker as pc

    granted: list = []

    async def _activate(executor, user_id, plan, months):
        granted.append(user_id)

    monkeypatch.setattr(pc, "_activate_subscription", _activate)
    conn = _ConfirmConn("PAY-FIRST")
    await pc._confirm(_ConfirmPool(conn), None, _pay(id=9, reference="PAY-SECOND"), "TXHASH")
    assert not granted, "одна транзакция блокчейна выдала второй счёт"
    assert not any("UPDATE payments" in q for q in conn.sql)


def test_strike_and_host_server_allocate_unique_amounts():
    """Ровные 250 USDT у всех покупателей Strike подтверждали чужой счёт."""
    assert "usdt_invoice.create_payment(" in _func_src("bot/handlers/strike.py", "cb_strike_buy")
    assert "usdt_invoice.create_payment(" in _func_src("services/host_server.py", "create_purchase")
    assert "usdt_invoice.create_payment(" in _func_src("bot/handlers/subscription.py", "cb_pay")


def test_ton_checker_reads_wallet_set_in_payment_settings(monkeypatch):
    """Кошелёк из «⚙️ Настройка оплаты» живёт в override, а не в env."""
    from bot.handlers import subscription as sh
    from services import payment_checker as pc

    monkeypatch.delenv("TON_WALLET", raising=False)
    monkeypatch.setattr(sh, "_PAY_OVERRIDES", {"TON_WALLET": "UQFROMDB"})
    assert pc._TON_WALLET() == "UQFROMDB"


# ── D3: единый гейт тарифа прогрева ─────────────────────────────────────────

@pytest.fixture()
def no_subscription(monkeypatch):
    import bot.utils.subscription as sub

    async def _no(pool, uid, min_plan):
        return False

    monkeypatch.setattr(sub, "require_plan", _no)


class _WarmPool:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        self.queries.append((q, a))
        return "UPDATE 1"

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        return []

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        return None


async def test_warmup_day_is_skipped_without_subscription(monkeypatch, no_subscription):
    """Подписка кончилась — цикл больше не греет, а честно пишет причину."""
    from services import account_warmer as aw

    async def _never(*a, **kw):
        raise AssertionError("без подписки дошли до аккаунта")

    monkeypatch.setattr(aw.db, "get_account_for_telethon", _never)
    pool = _WarmPool()
    plan = {"id": 77, "account_id": 5, "owner_id": 900, "current_day": 3,
            "daily_actions": 10}
    res = await aw.run_daily_warmup(pool, plan)
    assert res["actions_done"] == 0
    skips = [a for q, a in pool.queries if "last_skip_reason" in q]
    assert skips == [(77, "no_subscription")]


async def test_warmup_session_is_skipped_without_subscription(monkeypatch, no_subscription):
    from services import account_warmer as aw

    async def _never(*a, **kw):
        raise AssertionError("без подписки сессия пошла греть")

    monkeypatch.setattr(aw, "_get_warmup_resources", _never)
    res = await aw.run_warmup_session(_WarmPool(), {"id": 3, "owner_id": 900,
                                                    "account_ids": [1, 2]})
    assert res["actions_done"] == 0


async def test_plan_is_not_created_without_subscription(no_subscription):
    from services import account_warmer as aw
    from services.operation_bus import PlanRequiredError

    pool = _WarmPool()
    with pytest.raises(PlanRequiredError):
        await aw.create_warmup_plan(pool, 900, 5, "standard")
    assert not any("INSERT INTO account_warmup_plans" in q for q, _ in pool.queries)


async def test_plan_is_not_written_on_someone_elses_account(monkeypatch):
    """D5: аккаунт не этого владельца — INSERT…SELECT не даёт строки, отказ."""
    import bot.utils.subscription as sub
    from services import account_warmer as aw

    async def _yes(pool, uid, min_plan):
        return True

    monkeypatch.setattr(sub, "require_plan", _yes)
    pool = _WarmPool()
    with pytest.raises(PermissionError):
        await aw.create_warmup_plan(pool, 900, 5, "standard")
    sql = next(q for q, _ in pool.queries if "INSERT INTO account_warmup_plans" in q)
    assert "a.owner_id = $1" in sql
    assert "account_warmup_plans.owner_id = EXCLUDED.owner_id" in sql
    assert not any("UPDATE tg_accounts" in q for q, _ in pool.queries)


async def test_bulk_warmup_refuses_without_subscription(no_subscription):
    from services import mini_app_api as m

    pool = _WarmPool()
    with pytest.raises(PermissionError):
        await m._warmup_bulk_core(pool, 900)
    assert not any("INSERT INTO" in q for q, _ in pool.queries)


def test_account_card_checks_tariff_before_writing_the_plan():
    src = _func_src("services/mini_app_accounts.py", "start_warmup")
    assert src.index("enforce_gates(") < src.index("INSERT INTO account_warmup_plans")


def test_bot_paths_check_tariff_before_writing():
    h = "bot/handlers/account_warmup.py"
    for fn in ("cb_warmup_create_all_plans", "cb_wu_sess_start", "cb_warmup_pick_profile"):
        src = _func_src(h, fn)
        assert "_warmup_gate_refusal(" in src, fn
    pick = _func_src(h, "cb_warmup_pick_profile")
    assert pick.index("get_account_label(") < pick.index("_upsert_niche_profile(")


def test_no_subscription_skip_is_shown_honestly():
    from services import warmup_status as ws

    st = ws.classify({"status": "active", "last_skip_reason": "no_subscription",
                      "last_action_at": _now()})
    assert st["state"] == "stalled" and "Нет подписки" in st["label"]
    assert "продлите подписку" in st["hint"]


# ── D18: напоминание об окончании подписки ──────────────────────────────────

class _NoticePool:
    def __init__(self, expiring, expired):
        self.expiring = expiring
        self.expired = expired

    async def fetch(self, q, *a):
        return self.expiring if "is_active = true" in q else self.expired


class _Bot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


async def test_owner_is_told_before_and_after_the_end(monkeypatch):
    import database.db as db
    import bot.utils.subscription as sub
    from services import subscription_notices as sn

    seen: set = set()

    async def _dedup(pool, uid, key, cooldown):
        if (uid, key) in seen:
            return False
        seen.add((uid, key))
        return True

    monkeypatch.setattr(db, "notify_dedup_ok", _dedup)
    monkeypatch.setattr(sub, "is_platform_admin", lambda uid: False)
    pool = _NoticePool(
        expiring=[{"user_id": 1, "expires_at": _now() + datetime.timedelta(days=2)}],
        expired=[{"user_id": 2, "expires_at": _now() - datetime.timedelta(hours=5)}],
    )
    bot = _Bot()
    assert await sn.run_once(pool, bot) == {"expiring": 1, "expired": 1}
    texts = dict(bot.sent)
    assert "скоро закончится" in texts[1]
    assert "закончилась" in texts[2]
    # Второй проход в тот же день — без повторов.
    assert await sn.run_once(pool, bot) == {"expiring": 0, "expired": 0}
