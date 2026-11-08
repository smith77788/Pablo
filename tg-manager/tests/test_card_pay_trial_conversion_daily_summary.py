"""Пробелы документа «интеграции/аналитика/монетизация», найденные при сверке.

РАЗРЫВ 1 (п. 4). Infragram можно было оплатить только криптой или звёздами:
картой — никак, хотя у лидеров карта — основной способ. Теперь есть счёт
Telegram Payments через платёжного провайдера (`CARD_PROVIDER_TOKEN`,
`CARD_CURRENCY`, `CARD_PER_USD`); без всех трёх настроек кнопки нет.

РАЗРЫВ 2 (п. 2). Сумма в TON держалась сутки, а курс ходит на проценты за
час. Теперь цена в TON действует 30 минут.

РАЗРЫВ 3 (п. 6). Пробный период выдавался, но никто не видел, сколько
пробников потом платят. Теперь `trial.conversion` и /trial_stats.

РАЗРЫВ 4 (п. 10). Сводка приходила только раз в неделю. Теперь её можно
получить в любой момент (/summary) и включить на каждое утро кнопкой.
"""
from __future__ import annotations

import asyncio
import datetime
import os

import pytest
from bot.callbacks import SnoozeCb, SubCb

from tests.test_subscription_stars_and_ton_rate import _buttons, _Callback, _RecPool

UTC = datetime.UTC
_CARD = {"CARD_PROVIDER_TOKEN": "381764678:TEST:12345", "CARD_CURRENCY": "rub",
         "CARD_PER_USD": "95"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from bot.handlers import subscription as sh
    for k in (*_CARD, "STARS_PER_USD"):
        monkeypatch.delenv(k, raising=False)
        sh._PAY_OVERRIDES.pop(k, None)
    yield
    for k in (*_CARD, "STARS_PER_USD"):
        sh._PAY_OVERRIDES.pop(k, None)


# ── Карта ───────────────────────────────────────────────────────────────────

def test_card_is_off_until_token_currency_and_rate_are_all_set():
    from bot.handlers import subscription as sh

    assert sh.card_config() is None
    sh.set_pay_config({"CARD_PROVIDER_TOKEN": "tok-123456789", "CARD_CURRENCY": "RUB"})
    assert sh.card_config() is None, "без курса карта не включается"
    sh.set_pay_config({"CARD_PER_USD": "95"})
    assert sh.card_config() == ("tok-123456789", "RUB", 95.0)
    sh.set_pay_config({"CARD_CURRENCY": "RUBLES"})
    assert sh.card_config() is None, "валюта — три буквы ISO"
    assert sh.card_amount(29, 95) == 275500
    assert sh.card_amount(26.1, 95) == 248000, "2479.5 → вверх до рубля"
    assert sh.card_amount(29, 0) == 0


async def test_card_button_and_invoice(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "")
    monkeypatch.setattr(sh, "_tron_wallet", lambda: "")

    cb = _Callback()
    await sh.cb_choose_period(cb, SubCb(action="choose_period", plan="paid", months=1,
                                        tier="pro"), _RecPool())
    assert not any("Картой" in t for t in _buttons(cb.message.edits[-1][1]))

    sh.set_pay_config(_CARD)
    cb = _Callback()
    await sh.cb_choose_period(cb, SubCb(action="choose_period", plan="paid", months=1,
                                        tier="pro"), _RecPool())
    texts = _buttons(cb.message.edits[-1][1])
    assert any(t.startswith("💳 Картой") and "RUB" in t for t in texts), texts
    assert not any("Запросить подписку" in t for t in texts), (
        "с картой оплата уже автоматическая")

    cb = _Callback()
    pool = _RecPool()
    await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency=sh.CARD,
                              tier="pro"), pool)
    ins = [a for q, a in pool.queries if "INSERT INTO payments" in q]
    assert ins, "счёт на карту не записан"
    _uid, _plan, _m, cur, amount, usd, wallet, ref, tier = ins[0]
    assert (cur, wallet, tier) == ("RUB", sh.CARD_WALLET, "pro")
    assert amount == sh.card_amount(usd, 95)
    inv = cb.message.invoices[0]
    assert inv["provider_token"] == _CARD["CARD_PROVIDER_TOKEN"]
    assert inv["currency"] == "RUB"
    assert inv["prices"][0].amount == amount
    assert sh.parse_stars_payload(inv["payload"])[3] == ref


def test_precheckout_checks_the_currency_of_the_invoice():
    from bot.handlers.subscription import precheckout_verdict as v

    row = {"status": "pending", "amount_crypto": 275500, "currency": "RUB",
           "expires_at": datetime.datetime.now(UTC) + datetime.timedelta(hours=1)}
    assert v(row, 275500, "RUB") is None
    assert v(row, 275500, "XTR"), "счёт в рублях не оплачивается звёздами"
    assert v(row, 275500, "USD")


def test_card_token_is_stored_encrypted_and_never_shown():
    import inspect

    from bot.handlers import subscription as sh

    src = inspect.getsource(sh)
    assert "encrypt_token" in src, "токен провайдера должен храниться зашифрованным"
    assert sh._mask("381764678:TEST:12345", secret=True) == "✅ задан"


# ── TON: цена действует 30 минут ────────────────────────────────────────────

async def test_ton_quote_lives_thirty_minutes(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "UQTONWALLET123")

    async def _rate(http=None):
        return 4.0

    monkeypatch.setattr(sh, "_live_ton_rate", _rate)
    cb = _Callback()
    pool = _RecPool()
    await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency="TON"), pool)
    q, args = next((q, a) for q, a in pool.queries if "INSERT INTO payments" in q)
    assert "make_interval(mins =>" in q and "24 hours" not in q
    assert sh.TON_QUOTE_MINUTES in args and sh.TON_QUOTE_MINUTES == 30
    assert "30 минут" in cb.message.edits[-1][0]


# ── Сводка за день ──────────────────────────────────────────────────────────

TUESDAY_9 = datetime.datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def test_daily_window_skips_monday_and_night():
    from services import weekly_report as wr

    assert wr.in_daily_window(TUESDAY_9)
    assert not wr.in_daily_window(TUESDAY_9 - datetime.timedelta(days=1)), (
        "в понедельник приходит недельный отчёт, второй не нужен")
    assert not wr.in_daily_window(TUESDAY_9.replace(hour=3))
    assert wr.day_key(TUESDAY_9) == "daily_report:2026-11-03"


def test_daily_text_compares_to_yesterday():
    from services import weekly_report as wr

    text = wr.format_report(
        {"headline": "x", "has_prev": False, "sections": [],
         "recommendations": [{"title": "Шаг"}]},
        [{"currency": "USD", "orders": 2, "paid": 1, "revenue": 1000, "revenue_prev": 500}],
        daily=True)
    assert "Сводка за день" in text and "Отчёт за неделю" not in text
    assert "к вчера" in text and "Что сделать сегодня" in text
    assert "первая сводка" in text


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


def _patch_daily(monkeypatch, *, enabled):
    from database import db
    from services.organism import digest, spine

    keys = []

    async def _state_get(pool, owner_id, key, default=None):
        return enabled if key == "daily_report" else default

    async def _dedup(pool, uid, key, cooldown):
        keys.append(key)
        return True

    async def _build(pool, owner_id, *, state_key="digest_metrics"):
        assert state_key == "digest_metrics_daily", "база трендов сводки — своя"
        return {"headline": "ok", "has_prev": True, "sections": [], "recommendations": []}

    monkeypatch.setattr(spine, "state_get", _state_get)
    monkeypatch.setattr(db, "notify_dedup_ok", _dedup)
    monkeypatch.setattr(digest, "build", _build)
    return keys


def test_daily_summary_is_sent_only_when_turned_on(monkeypatch):
    from services import weekly_report as wr

    run = asyncio.new_event_loop().run_until_complete
    _patch_daily(monkeypatch, enabled=False)
    bot = _Bot()
    assert run(wr.maybe_send_daily(object(), bot, 42, now=TUESDAY_9)) is False
    assert not bot.sent, "без согласия владельца в личку — только раз в неделю"

    keys = _patch_daily(monkeypatch, enabled=True)
    assert run(wr.maybe_send_daily(object(), bot, 42, now=TUESDAY_9)) is True
    assert keys == ["daily_report:2026-11-03"]
    _chat, text, kw = bot.sent[0]
    assert "Сводка за день" in text
    btns = [b for row in kw["reply_markup"].inline_keyboard for b in row]
    assert any("Не присылать каждый день" in b.text for b in btns)


def test_weekly_report_offers_to_turn_daily_on():
    from services import weekly_report as wr

    btns = [b for row in wr._kb(False).inline_keyboard for b in row]
    daily = next(b for b in btns if "каждый день" in b.text)
    assert SnoozeCb.unpack(daily.callback_data) == SnoozeCb(
        action="daily", sid="daily_report", code="on")


def test_runner_sends_daily_and_bot_has_summary_command():
    import inspect

    from bot.handlers import organism_nudge
    from services.organism import runner

    assert "weekly_report.maybe_send_daily(pool, bot, oid)" in inspect.getsource(runner._tick)
    assert hasattr(organism_nudge, "cmd_summary") and hasattr(organism_nudge, "cb_daily_report")


class _CbMsg:
    def __init__(self):
        self.markups = []

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


class _Cb:
    def __init__(self):
        self.from_user = type("U", (), {"id": 42})()
        self.message = _CbMsg()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


async def test_daily_toggle_saves_and_flips_the_button(monkeypatch):
    from bot.handlers import organism_nudge
    from services.organism import spine

    saved = {}

    async def _set(pool, owner_id, key, value):
        saved[key] = value

    monkeypatch.setattr(spine, "state_set", _set)
    cb = _Cb()
    await organism_nudge.cb_daily_report(cb, SnoozeCb(action="daily", sid="daily_report",
                                                      code="on"), object())
    assert saved == {"daily_report": True}
    btns = [b for row in cb.message.markups[-1].inline_keyboard for b in row]
    assert any("Не присылать каждый день" in b.text for b in btns)
    assert "каждое утро" in cb.answers[-1]


# ── Живой Postgres ──────────────────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
UID = 99_000_811


def test_card_payment_confirms_with_its_currency_on_real_schema():
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
                       amount_crypto, amount_usd, wallet_address, reference, expires_at, tier)
                   VALUES ($1,'paid',1,'RUB',275500,29,$2,'PAY-CARD01',
                           now() + INTERVAL '1 hour','pro')""", UID, sh.CARD_WALLET)
            payload = f"{UID}:paid:1:PAY-CARD01"
            row = await sh._stars_invoice_row(pool, payload, UID)
            verdict = sh.precheckout_verdict(row, 275500, "RUB")
            wrong = await sh.confirm_stars_payment(pool, UID, payload, "ch-x", 275500, "XTR")
            ok = await sh.confirm_stars_payment(pool, UID, payload, "ch-1", 275500, "RUB")
            tier = await pool.fetchval(
                "SELECT tier FROM subscriptions WHERE user_id=$1 AND is_active", UID)
            return verdict, wrong, ok, tier
        finally:
            await pool.execute("DELETE FROM payments WHERE user_id=$1", UID)
            await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", UID)
            await pool.close()

    verdict, wrong, ok, tier = asyncio.new_event_loop().run_until_complete(go())
    assert verdict is None, verdict
    assert wrong is None, "счёт в рублях подтвердился платежом в звёздах"
    assert ok is not None and tier == "pro"


def test_trial_conversion_on_real_schema():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from services import trial

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            return await trial.conversion(pool, days=90)
        finally:
            await pool.close()

    res = asyncio.new_event_loop().run_until_complete(go())
    assert set(res) >= {"granted", "running", "converted", "rate", "median_days"}


def test_daily_sales_query_runs_on_real_schema():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from services import weekly_report as wr

    owner = 99_000_812

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", owner)
            for cents, hours in ((30000, 3), (10000, 30)):
                await pool.execute(
                    """INSERT INTO bot_sales_orders(owner_id, bot_id, customer_chat_id,
                           total_cents, currency, status, created_at)
                       VALUES($1, 1, 2, $2, 'USD', 'done', now() - make_interval(hours => $3))""",
                    owner, cents, hours)
            return (await wr.sales_week(pool, owner, 1),
                    await wr.scenarios_week(pool, owner, 1))
        finally:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", owner)
            await pool.close()

    sales, scen = asyncio.new_event_loop().run_until_complete(go())
    assert sales == [{"currency": "USD", "orders": 1, "paid": 1,
                      "revenue": 30000, "revenue_prev": 10000}]
    assert scen == []
