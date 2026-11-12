"""Квота запросов кода входа: один владелец не кладёт вход всей платформе.

Ключи приложения (TG_API_ID/TG_API_HASH) общие для всех владельцев, и
Telegram наказывает их целиком (API_ID_PUBLISHED_FLOOD). До квоты единственным
ограничением был общий лимитер HTTP — 120 запросов в минуту, то есть тысячи
кодов в час на чужие номера с одного владельца, а авторег из воркера мимо
HTTP не держало ничего. Квота стоит в `start_login` — единственной двери
всех входов по номеру (бот, мини-апп, авторег).
"""
from __future__ import annotations

import asyncio

import pytest

from bot.handlers import auto_registrar as ar
from services import account_manager as am


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(am, "_send_code_log", {})
    monkeypatch.setattr(am, "_SEND_CODE_PER_OWNER", 3)


def test_owner_hits_the_ceiling_with_a_russian_reason():
    for _ in range(3):
        am._take_send_code_slot(7, now=1000.0)
    with pytest.raises(am.SendCodeQuotaError) as ei:
        am._take_send_code_slot(7, now=1001.0)
    msg = am.login_error_ru(ei.value)
    assert "предел" in msg and "мин" in msg
    assert not any("a" <= ch.lower() <= "z" for ch in msg.replace("Telegram", "")), msg


def test_quota_is_per_owner_and_slides():
    for _ in range(3):
        am._take_send_code_slot(7, now=1000.0)
    am._take_send_code_slot(8, now=1000.0)          # другой владелец не задет
    am._take_send_code_slot(7, now=1000.0 + 3601)   # окно прошло — снова можно


def test_start_login_spends_the_quota_before_touching_telegram(monkeypatch):
    touched = []

    def _no_client(*a, **k):
        touched.append(1)
        raise AssertionError("до Telegram дойти не должно было")

    monkeypatch.setattr(am, "_make_client", _no_client)
    for _ in range(3):
        am._take_send_code_slot(9)
    with pytest.raises(am.SendCodeQuotaError):
        asyncio.run(am.start_login("+79990000000", owner_id=9))
    assert not touched


def test_autoreg_batch_stops_on_the_quota():
    verdict = ar.classify_login_failure(am.SendCodeQuotaError("предел, повторите через 5 мин"))
    assert verdict["stop"] is True
    assert "предел" in verdict["text"]
