"""AUTH_KEY_DUPLICATED в проверке здоровья: честный НЕ-деактивирующий статус.

Симптом из лога op_128_check_accounts_health.csv: КАЖДЫЙ аккаунт помечен
«ok / ✅ активен», хотя внутри ошибка «...used under two different IP addresses
simultaneously, and can no longer be used» — dup падал в общий фолбэк и
классифицировался как active. Оператор не видел, что флот в конфликте.

Правильно: НЕ лжём «активен» (это 'cooldown' с причиной), но и НЕ деактивируем
(session_expired+auth_error → is_active=FALSE; разовый флап не должен выключать
флот — осознанное прежнее решение, см. test_auth_key_duplicated_not_dead).
"""
from __future__ import annotations

import asyncio

from services import account_manager as am
from telethon.errors import AuthKeyDuplicatedError


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _DupClient:
    async def connect(self):
        raise AuthKeyDuplicatedError()

    async def disconnect(self):
        pass


def test_dup_is_cooldown_not_active_not_deactivating(monkeypatch):
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _DupClient())
    res = _run(am.check_account_status_full("s" * 20, {"id": 1}, check_spambot=False))
    # не лжём «активен»
    assert res["status"] == "cooldown"
    assert res["status"] != "active"
    # и НЕ деактивируем: нет auth_error → монитор не ставит is_active=FALSE
    assert not res.get("auth_error")
    assert res.get("session_conflict") is True
    assert "AUTH_KEY_DUPLICATED" in res["reason"]


class _LiveClient:
    def __init__(self):
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected and not self.disconnected

    async def disconnect(self):
        self.disconnected = True

    async def get_me(self):
        return type("Me", (), {"id": 7, "first_name": "T", "username": "t",
                               "last_name": "", "phone": "1", "premium": False,
                               "photo": None})()


def test_status_check_disconnects_no_leak(monkeypatch):
    # Регресс: раньше клиент НИКОГДА не отключался → живой коннект висел с IP
    # проверки, а операция коннектила ту же сессию с др. IP → AUTH_KEY_DUPLICATED.
    c = _LiveClient()
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: c)
    res = _run(am.check_account_status_full("s" * 20, {"id": 1}, check_spambot=False))
    assert res["status"] == "active"
    assert c.disconnected is True, "коннект проверки статуса должен закрываться"
    # мьютекс сессии освобождён (следующий коннект той же сессии проходит)
    assert am._try_acquire_session(am._session_key("s" * 20, {"id": 1})) is True
    am._release_session(am._session_key("s" * 20, {"id": 1}))


def test_status_check_skips_when_session_busy(monkeypatch):
    # Аккаунт занят операцией (мьютекс держится) → проверка НЕ открывает вторую
    # сессию, честно докладывает 'active' (занят = жив), без коннекта.
    made = {"n": 0}

    def _make(*a, **k):
        made["n"] += 1
        return _LiveClient()

    monkeypatch.setattr(am, "_make_client", _make)
    key = am._session_key("busy" * 30, {"id": 2})
    assert am._try_acquire_session(key) is True     # имитируем занятость операцией
    try:
        res = _run(am.check_account_status_full("busy" * 30, {"id": 2}, check_spambot=False))
        assert res["status"] == "active"
        assert res.get("session_busy") is True
        assert made["n"] == 0, "второй коннект к занятой сессии открываться не должен"
    finally:
        am._release_session(key)
