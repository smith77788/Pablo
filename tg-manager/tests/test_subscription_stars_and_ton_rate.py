"""Оплата подписки Infragram: живой курс TON и Telegram Stars.

РАЗРЫВ 1. Цена в TON считалась по `TON_RATE`, который админ вводит руками, с
зашитым значением по умолчанию 3.0. Курс TON ходит на десятки процентов, и
счёт выставлялся по цифре, которую никто не обновлял: при росте TON человек
переплачивал, при падении платформа недополучала. Теперь курс берётся с рынка
(`services.ton_rate`), ручной — только запасной, а без курса TON не
предлагается вовсе.

РАЗРЫВ 2. Оплатить Infragram можно было только криптой. Теперь есть счёт в
Telegram Stars: строка `payments` с currency='XTR', pre_checkout сверяет счёт,
successful_payment включает подписку один раз на платёж.
"""
from __future__ import annotations

import asyncio
import datetime
import os

import pytest

from bot.callbacks import SubCb

UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def _clean_rate(monkeypatch):
    from services import ton_rate
    ton_rate._reset_for_tests()
    monkeypatch.delenv("TON_RATE", raising=False)
    monkeypatch.delenv("STARS_PER_USD", raising=False)
    from bot.handlers import subscription as sh
    sh._PAY_OVERRIDES.pop("STARS_PER_USD", None)
    yield
    ton_rate._reset_for_tests()
    sh._PAY_OVERRIDES.pop("STARS_PER_USD", None)


# ── Курс TON ────────────────────────────────────────────────────────────────

def test_rate_parsers_read_real_answers_and_reject_garbage():
    from services import ton_rate

    assert ton_rate.parse_tonapi({"rates": {"TON": {"prices": {"USD": 5.42}}}}) == 5.42
    assert ton_rate.parse_coingecko({"the-open-network": {"usd": 3.1}}) == 3.1
    for bad in ({}, {"rates": {}}, {"rates": {"TON": {"prices": {"USD": 0}}}},
                {"rates": {"TON": {"prices": {"USD": 1e6}}}}, None, "x"):
        assert ton_rate.parse_tonapi(bad) is None, bad


def test_no_rate_anywhere_means_no_rate_not_three_dollars():
    """Зашитые 3.0 больше не подставляются: курса нет — так и говорим."""
    from bot.handlers import subscription as sh
    from services import ton_rate

    assert ton_rate.cached() == (None, "")
    assert sh._get_ton_rate() == 0.0
    with pytest.raises(ValueError):
        sh._calc("paid", 1, "TON")


def test_live_rate_wins_over_manual_and_falls_back_to_it(monkeypatch):
    from services import ton_rate

    monkeypatch.setenv("TON_RATE", "2.00")
    calls = []

    async def _live(http):
        calls.append(1)
        return (5.0, "tonapi") if len(calls) == 1 else (None, "")

    monkeypatch.setattr(ton_rate, "_fetch_live", _live)
    t0 = 1_000_000.0

    async def go():
        return [
            await ton_rate.get_rate(object(), now=t0),
            # свежий кэш — рынок не спрашиваем
            await ton_rate.get_rate(object(), now=t0 + 60),
            # кэш несвежий, рынок лёг — держимся за последний рыночный
            await ton_rate.get_rate(object(), now=t0 + ton_rate.FRESH_S + 1),
            # рынка нет дольше STALE_MAX_S — только тогда ручной
            await ton_rate.get_rate(object(), now=t0 + ton_rate.STALE_MAX_S + 1),
        ]

    got = asyncio.new_event_loop().run_until_complete(go())
    assert got[0] == (5.0, "tonapi")
    assert got[1] == (5.0, "tonapi")
    assert len(calls) == 3, "при свежем кэше рынок спрашивать не должны"
    assert got[2] == (5.0, "tonapi")
    assert got[3] == (2.0, "manual")


def test_ton_price_uses_the_rate_of_the_invoice():
    from bot.handlers.subscription import _calc

    usd, ton = _calc("paid", 1, "TON", 0, 5.0)
    assert ton == round(usd / 5.0, 2)


class _Msg:
    def __init__(self):
        self.edits: list[tuple[str, dict]] = []
        self.invoices: list[dict] = []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))

    async def answer_invoice(self, **kw):
        self.invoices.append(kw)


class _User:
    id = 555


class _Callback:
    def __init__(self):
        self.from_user = _User()
        self.data = "sub:x"
        self.message = _Msg()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class _RecPool:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        return None

    async def execute(self, q, *a):
        self.queries.append((q, a))
        return "INSERT 0 1"

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        return []


def _buttons(kw) -> list[str]:
    markup = kw.get("reply_markup")
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


async def test_ton_is_hidden_when_there_is_no_rate(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "UQTONWALLET123")
    monkeypatch.setattr(sh, "_tron_wallet", lambda: "TWALLET123456")

    async def _no_rate(http=None):
        return None

    monkeypatch.setattr(sh, "_live_ton_rate", _no_rate)
    cb = _Callback()
    await sh.cb_choose_period(cb, SubCb(action="choose_period", plan="paid", months=1), _RecPool())
    texts = _buttons(cb.message.edits[-1][1])
    assert not any("TON —" in t for t in texts), texts
    assert any("USDT" in t for t in texts), "USDT от курса не зависит и должен остаться"


async def test_ton_invoice_is_not_created_without_rate(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "UQTONWALLET123")

    async def _no_rate(http=None):
        return None

    monkeypatch.setattr(sh, "_live_ton_rate", _no_rate)
    cb = _Callback()
    pool = _RecPool()
    await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency="TON"), pool)
    assert not any("INSERT INTO payments" in q for q, _ in pool.queries)
    assert "Курс TON" in cb.message.edits[-1][0]


async def test_ton_invoice_locks_the_live_rate(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "UQTONWALLET123")

    async def _rate(http=None):
        return 4.0

    monkeypatch.setattr(sh, "_live_ton_rate", _rate)
    cb = _Callback()
    pool = _RecPool()
    await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency="TON"), pool)
    ins = [a for q, a in pool.queries if "INSERT INTO payments" in q]
    assert ins, "счёт в TON не создан"
    usd = ins[0][5]
    assert ins[0][4] == round(usd / 4.0, 2), "в счёт записан не тот курс"


# ── Stars ───────────────────────────────────────────────────────────────────

def test_stars_are_off_until_the_owner_sets_a_price():
    from bot.handlers import subscription as sh

    assert sh._stars_per_usd() == 0
    assert sh.stars_amount(29.0) == 0
    sh.set_pay_config({"STARS_PER_USD": "77"})
    assert sh._stars_per_usd() == 77
    assert sh.stars_amount(29.0) == 2233
    assert sh.stars_amount(26.1, 77) == 2010  # 2009.7 → вверх
    sh.set_pay_config({"STARS_PER_USD": "5000"})
    assert sh._stars_per_usd() == 0, "нелепый курс звезды не должен включать Stars"


def test_stars_payload_round_trips_and_stays_readable_by_recovery():
    from bot.handlers.subscription import parse_stars_payload
    from services.payment_recovery import parse_payload

    payload = "555:paid:3:PAY-AB12CD"
    assert parse_stars_payload(payload) == (555, "paid", 3, "PAY-AB12CD")
    assert parse_payload(payload) == (555, "paid", 3), (
        "восстановление подписок по истории звёзд читает первые три поля")
    for bad in ("", "555:paid:3", "x:paid:3:PAY-AB12CD", "555:paid:3:PAY-ab12cd; drop"):
        assert parse_stars_payload(bad) is None, bad


def _row(**kw):
    base = {"status": "pending", "amount_crypto": 2233,
            "expires_at": datetime.datetime.now(UTC) + datetime.timedelta(hours=1)}
    base.update(kw)
    return base


def test_precheckout_rejects_everything_but_the_exact_open_invoice():
    from bot.handlers.subscription import precheckout_verdict as v

    assert v(_row(), 2233, "XTR") is None
    assert v(None, 2233, "XTR")
    assert v(_row(), 2233, "USD")
    assert v(_row(), 1, "XTR"), "сумма не та, что в счёте"
    assert v(_row(status="confirmed"), 2233, "XTR"), "оплаченный счёт второй раз"
    past = datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)
    assert v(_row(expires_at=past), 2233, "XTR"), "просроченный счёт"


async def test_stars_button_and_invoice(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "")
    monkeypatch.setattr(sh, "_tron_wallet", lambda: "")
    sh.set_pay_config({"STARS_PER_USD": "77"})
    cb = _Callback()
    await sh.cb_choose_period(cb, SubCb(action="choose_period", plan="paid", months=1), _RecPool())
    texts = _buttons(cb.message.edits[-1][1])
    assert any("Stars" in t for t in texts), texts
    assert not any("Запросить подписку" in t for t in texts), (
        "при включённых Stars оплата уже автоматическая")

    cb = _Callback()
    pool = _RecPool()
    await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency="XTR"), pool)
    ins = [a for q, a in pool.queries if "INSERT INTO payments" in q]
    assert ins and ins[0][3] == "XTR"
    inv = cb.message.invoices[0]
    assert inv["currency"] == "XTR"
    assert inv["prices"][0].amount == ins[0][4] == sh.stars_amount(ins[0][5])
    assert sh.parse_stars_payload(inv["payload"])[3] == ins[0][6]


def test_gate_lets_successful_payment_through():
    from aiogram.types import Chat, Message, SuccessfulPayment, User

    from bot.middlewares import subscription_gate as gate

    gate.set_gate_enabled(True)
    gate.set_gate_channels([{"channel_username": "@x", "channel_title": "x"}])
    try:
        msg = Message(
            message_id=1, date=datetime.datetime.now(UTC),
            chat=Chat(id=555, type="private"),
            from_user=User(id=555, is_bot=False, first_name="a"),
            successful_payment=SuccessfulPayment(
                currency="XTR", total_amount=10, invoice_payload="555:paid:1:PAY-AAAAAA",
                telegram_payment_charge_id="c1", provider_payment_charge_id=""),
        )
        seen = []

        async def handler(event, data):
            seen.append(event)

        asyncio.new_event_loop().run_until_complete(
            gate.SubscriptionGateMiddleware()(handler, msg, {"bot": object()}))
        assert seen, "гейт подписки на каналы проглотил подтверждение оплаты"
    finally:
        gate.set_gate_enabled(False)
        gate.set_gate_channels([])


# ── Живой Postgres: миграция и одно подтверждение на платёж ─────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
UID = 99_000_555


def test_stars_payment_confirms_once_on_real_schema():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    from bot.handlers import subscription as sh

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM payments WHERE user_id=$1", UID)
            await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", UID)
            await pool.execute(
                """INSERT INTO payments (user_id, plan, period_months, currency,
                       amount_crypto, amount_usd, wallet_address, reference, expires_at)
                   VALUES ($1,'paid',1,'XTR',2233,29,'telegram_stars','PAY-STAR01',
                           now() + INTERVAL '1 hour')""", UID)
            payload = f"{UID}:paid:1:PAY-STAR01"
            first = await sh.confirm_stars_payment(pool, UID, payload, "charge-1", 2233)
            again = await sh.confirm_stars_payment(pool, UID, payload, "charge-1", 2233)
            other = await sh.confirm_stars_payment(pool, UID + 1, payload, "charge-2", 2233)
            sub = await pool.fetchrow(
                "SELECT expires_at FROM subscriptions WHERE user_id=$1", UID)
            pay = await pool.fetchrow(
                "SELECT status, tx_hash FROM payments WHERE reference='PAY-STAR01'")
            return first, again, other, sub, pay
        finally:
            await pool.execute("DELETE FROM payments WHERE user_id=$1", UID)
            await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", UID)
            await pool.close()

    first, again, other, sub, pay = asyncio.new_event_loop().run_until_complete(go())
    assert first is not None, "оплата Stars не включила подписку"
    assert again is None, "повторная доставка апдейта продлила подписку второй раз"
    assert other is None, "чужой пользователь подтвердил чужой счёт"
    assert sub["expires_at"] == first
    assert (pay["status"], pay["tx_hash"]) == ("confirmed", "charge-1")
