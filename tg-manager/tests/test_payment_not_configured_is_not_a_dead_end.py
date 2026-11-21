"""Без TRON_WALLET покупка модуля не должна быть тупиком.

«⚔️ Массовые жалобы» и «🖥️ Host-Server» писали «Свяжитесь с
администратором» без кнопки и без контакта (дефект 10 сверки оплаты). Теперь
там «📩 Запросить доступ», как у подписки: заявка уходит платформенным
админам, а у Host-Server в заявке есть кнопка выдачи — другого ручного пути
выдать этот модуль нет.
"""
from __future__ import annotations

import pytest

from bot.callbacks import HostCb, StrikeCb


class _User:
    def __init__(self, uid=4242, username="buyer<x>"):
        self.id = uid
        self.username = username
        self.first_name = "B"


class _Msg:
    def __init__(self):
        self.edits: list[tuple] = []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw.get("reply_markup")))


class _Bot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


class _Callback:
    def __init__(self, uid=4242):
        self.from_user = _User(uid)
        self.message = _Msg()
        self.bot = _Bot()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def _recipients(monkeypatch, ids):
    from bot.utils import subscription as su

    async def _r(pool):
        return set(ids)

    monkeypatch.setattr(su, "platform_alert_recipients", _r)


# ── Массовые жалобы ─────────────────────────────────────────────────────────

async def test_strike_without_wallet_offers_request_button(monkeypatch):
    from bot.handlers import strike

    async def _no(pool, uid):
        return False

    monkeypatch.setattr(strike, "_has_access", _no)
    monkeypatch.setattr(strike, "_tron_wallet", lambda: "")
    cb = _Callback()
    await strike.cb_strike_buy(cb, pool=None)
    text, markup = cb.message.edits[-1]
    assert "Свяжитесь с администратором" not in text
    datas = [b.callback_data for b in _buttons(markup)]
    assert StrikeCb(action="request").pack() in datas
    # Кнопки «Проверить оплату» без счёта быть не должно — проверять нечего.
    assert StrikeCb(action="check_pay").pack() not in datas


async def test_strike_request_reaches_admins(monkeypatch):
    from bot.handlers import strike

    async def _no(pool, uid):
        return False

    monkeypatch.setattr(strike, "_has_access", _no)
    _recipients(monkeypatch, [1, 2])
    cb = _Callback()
    await strike.cb_strike_request(cb, pool=None)
    assert [s[0] for s in cb.bot.sent] == [1, 2]
    note = cb.bot.sent[0][1]
    assert "<code>4242</code>" in note and "Выдать доступ к жалобам" in note
    assert "buyer<x>" not in note, "имя пользователя ушло админу без экранирования"
    assert "Запрос отправлен" in cb.message.edits[-1][0]


async def test_strike_request_with_no_admin_does_not_claim_sent(monkeypatch):
    from bot.handlers import strike

    async def _no(pool, uid):
        return False

    monkeypatch.setattr(strike, "_has_access", _no)
    _recipients(monkeypatch, [])
    cb = _Callback()
    await strike.cb_strike_request(cb, pool=None)
    assert "Запрос отправлен" not in cb.message.edits[-1][0]


# ── Host-Server ─────────────────────────────────────────────────────────────

async def test_host_without_wallet_offers_request_button(monkeypatch):
    from bot.handlers import host_server as h

    async def _purchase(pool, uid):
        return {"already": False, "wallet": "NOT_CONFIGURED", "price_usd": 99,
                "reference": None, "amount_usdt": None}

    monkeypatch.setattr(h.hs, "create_purchase", _purchase)
    cb = _Callback()
    await h.cb_buy(cb, pool=None)
    text, markup = cb.message.edits[-1]
    assert "Свяжитесь с администратором" not in text
    datas = [b.callback_data for b in _buttons(markup)]
    assert HostCb(action="request").pack() in datas


async def test_host_request_carries_grant_button(monkeypatch):
    from bot.handlers import host_server as h

    async def _no(pool, uid):
        return False

    async def _price(pool):
        return 99

    monkeypatch.setattr(h.hs, "has_access", _no)
    monkeypatch.setattr(h.hs, "get_price", _price)
    _recipients(monkeypatch, [7])
    cb = _Callback()
    await h.cb_request(cb, pool=None)
    (chat, note, markup), = cb.bot.sent
    assert chat == 7 and "<code>4242</code>" in note
    datas = [b.callback_data for b in _buttons(markup)]
    assert HostCb(action="grant", val="4242").pack() in datas
    assert "Запрос отправлен" in cb.message.edits[-1][0]


@pytest.mark.parametrize("admin", [True, False])
async def test_host_grant_is_admin_only(monkeypatch, admin):
    from bot.handlers import host_server as h

    granted: list = []

    async def _grant(executor, uid, payment_ref=None, granted_by=None):
        granted.append((uid, granted_by))

    monkeypatch.setattr(h.hs, "grant_access", _grant)
    monkeypatch.setattr(h, "is_platform_admin", lambda uid: admin)
    cb = _Callback(uid=1)
    await h.cb_grant(cb, HostCb(action="grant", val="4242"), pool=None)
    if admin:
        assert granted == [(4242, 1)]
        assert cb.bot.sent and cb.bot.sent[0][0] == 4242
    else:
        assert granted == []
        assert cb.answers and cb.answers[0][1] is True
