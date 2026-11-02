"""Подписка звёздами с автопродлением.

РАЗРЫВ. Оплата Stars была только разовой: через месяц подписка кончалась,
владелец должен был сам вспомнить, зайти и заплатить снова — у лидеров
подписка продлевается сама. Теперь «Stars с автопродлением» выставляет
ссылку-подписку Telegram (subscription_period 30 дней), каждое автосписание
продлевает подписку на месяц одной транзакцией (своя строка payments,
повтор доставки отсекается по id списания), а отключить его можно кнопкой
на экране подписки.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from bot.callbacks import SubCb
from tests.test_subscription_stars_and_ton_rate import _buttons, _Callback, _RecPool


class _Bot:
    def __init__(self):
        self.links = []
        self.cancels = []

    async def create_invoice_link(self, **kw):
        self.links.append(kw)
        return "https://t.me/$invoice-link"

    async def edit_user_star_subscription(self, **kw):
        self.cancels.append(kw)
        return True


async def test_autorenew_is_offered_for_a_month_and_issued_as_a_subscription_link(monkeypatch):
    from bot.handlers import subscription as sh

    monkeypatch.setattr(sh, "_ton_wallet", lambda: "")
    monkeypatch.setattr(sh, "_tron_wallet", lambda: "")
    monkeypatch.delenv("STARS_PER_USD", raising=False)
    sh.set_pay_config({"STARS_PER_USD": "50"})
    try:
        cb = _Callback()
        await sh.cb_choose_period(
            cb, SubCb(action="choose_period", plan="paid", months=1, tier="start"), _RecPool())
        texts = _buttons(cb.message.edits[-1][1])
        assert "🔁 Stars с автопродлением — 750 ⭐/мес" in texts, texts

        cb = _Callback()
        await sh.cb_choose_period(
            cb, SubCb(action="choose_period", plan="paid", months=3, tier="start"), _RecPool())
        assert not any("автопродлением" in t for t in _buttons(cb.message.edits[-1][1])), (
            "Telegram продлевает только помесячно — на 3 месяца автопродление не предлагаем")

        cb = _Callback()
        cb.bot = _Bot()
        pool = _RecPool()
        await sh.cb_pay(cb, SubCb(action="pay", plan="paid", months=1, currency=sh.STARS_SUB,
                                  tier="start"), pool)
        ins = [a for q, a in pool.queries if "INSERT INTO payments" in q][0]
        link = cb.bot.links[0]
        assert link["subscription_period"] == 30 * 86400 and link["currency"] == "XTR"
        assert link["prices"][0].amount == ins[3] == 750
        assert ins[5] == sh.STARS_SUB_WALLET and ins[7] == "start"
        assert sh.parse_stars_payload(link["payload"])[3] == ins[6]
        markup = cb.message.edits[-1][1]["reply_markup"]
        assert markup.inline_keyboard[0][0].url == "https://t.me/$invoice-link"
    finally:
        sh._PAY_OVERRIDES.pop("STARS_PER_USD", None)


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
UID = 99_000_966


def test_each_renewal_adds_a_month_once_and_can_be_cancelled():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from bot.handlers import subscription as sh

    payload = f"{UID}:paid:1:PAY-SUBS01"

    async def clean(pool):
        await pool.execute("DELETE FROM payments WHERE user_id=$1", UID)
        await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", UID)

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await clean(pool)
            await pool.execute(
                """INSERT INTO payments (user_id, plan, period_months, currency, amount_crypto,
                       amount_usd, wallet_address, reference, expires_at, tier)
                   VALUES ($1,'paid',1,'XTR',750,15,$2,'PAY-SUBS01',
                           now() + INTERVAL '1 hour', 'start')""", UID, sh.STARS_SUB_WALLET)
            first, _ = await sh.confirm_stars_payment(pool, UID, payload, "sub-charge-1", 750)
            active = await sh.active_stars_subscription(pool, UID)
            renewed = await sh.renew_stars_subscription(pool, UID, payload, "renew-charge-2", 750)
            again = await sh.renew_stars_subscription(pool, UID, payload, "renew-charge-2", 750)
            stranger = await sh.renew_stars_subscription(
                pool, UID + 1, f"{UID + 1}:paid:1:PAY-SUBS01", "renew-charge-3", 750)
            rows = await pool.fetch(
                "SELECT status, tier, recurring_of FROM payments WHERE user_id=$1 "
                "ORDER BY id", UID)
            tier = await pool.fetchval("SELECT tier FROM subscriptions WHERE user_id=$1", UID)

            cb = _Callback()
            cb.from_user.id = UID
            cb.bot = _Bot()
            await sh.cb_stars_cancel(cb, pool)
            after = await sh.active_stars_subscription(pool, UID)
            return first, active, renewed, again, stranger, rows, tier, cb, after
        finally:
            await clean(pool)
            await pool.close()

    first, active, renewed, again, stranger, rows, tier, cb, after = \
        asyncio.new_event_loop().run_until_complete(go())
    assert active["tx_hash"] == "sub-charge-1", "id первого списания — ключ отмены подписки"
    assert renewed is not None and 28 <= (renewed[0] - first).days <= 31, (
        "автосписание не продлило подписку на месяц")
    assert again is None, "повторная доставка автосписания продлила второй раз"
    assert stranger is None, "чужой пользователь продлил по чужой подписке"
    assert [(r["status"], r["tier"], r["recurring_of"]) for r in rows] == [
        ("confirmed", "start", None), ("confirmed", "start", "PAY-SUBS01")]
    assert tier == "start"
    assert cb.bot.cancels == [{"user_id": UID, "telegram_payment_charge_id": "sub-charge-1",
                               "is_canceled": True}]
    assert after is None and "Автопродление отключено" in cb.message.edits[-1][0]


def test_renewal_update_is_routed_to_the_renewal():
    import inspect

    from bot.handlers import subscription as sh

    src = inspect.getsource(sh.on_stars_paid)
    assert "sp.is_recurring" in src and "renew_stars_subscription(" in src
