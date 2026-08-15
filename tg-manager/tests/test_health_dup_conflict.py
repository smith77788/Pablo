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
