"""Синхронизация контактов: системный сбой ≠ протухшие сессии.

Пользователь только что залогинил 28 аккаунтов по номеру — и все 28 упали на
синхронизации с сообщением «устаревшие сессии/недоступные прокси». Это ложь: 28
свежих сессий не могут протухнуть одновременно. Когда падает ВЕСЬ флот одинаково —
это транспорт (сеть/прокси/CF-релей/IPv6), а не сессии. Тогда система обязана:
  1. НЕ метить аккаунты session_expired (ложная тревога, лишний релог);
  2. честно сказать «системный сбой подключения», а не гадать про сессии;
  3. показать сырой пример ошибки для диагностики.
Изолированный сбой (один-два аккаунта из многих) метится как раньше.
"""
from __future__ import annotations

import asyncio


class _FakePool:
    def __init__(self, accounts):
        self._accounts = accounts
        self.execs = []

    async def fetch(self, q, *a):
        return self._accounts

    async def fetchval(self, q, *a):
        return len(self._accounts)

    async def execute(self, q, *a):
        self.execs.append((q, a))


def _accounts(n):
    return [{"id": i, "phone": f"+7999000{i:04d}", "first_name": f"A{i}", "is_active": True}
            for i in range(1, n + 1)]


def test_systemic_failure_not_marked_and_honest():
    from services.contacts_hub import sync_service as ss

    async def all_fail(pool, owner, acc_id):
        return {"error": "сессия недействительна", "status": "expired",
                "raw": "AuthKeyUnregisteredError", "synced": 0}

    orig = ss.sync_account
    ss.sync_account = all_fail
    try:
        pool = _FakePool(_accounts(28))
        r = asyncio.run(ss.sync_all_accounts(pool, 555))
    finally:
        ss.sync_account = orig

    assert r["systemic"] is True
    assert not pool.execs, "системный сбой не должен метить аккаунты session_expired"
    msg = r["message"].lower()
    assert "систем" in msg, r["message"]
    assert "устарел" not in msg, "нельзя утверждать про устаревшие сессии при системном сбое"
    assert "AuthKeyUnregisteredError" in r["message"], "нет сырого примера для диагностики"


def test_isolated_failure_still_marked():
    from services.contacts_hub import sync_service as ss

    async def one_fail(pool, owner, acc_id):
        if acc_id == 3:
            return {"error": "сессия недействительна", "status": "expired", "raw": "x", "synced": 0}
        return {"synced": 5, "created": 5, "updated": 0}

    orig = ss.sync_account
    ss.sync_account = one_fail
    try:
        pool = _FakePool(_accounts(6))
        r = asyncio.run(ss.sync_all_accounts(pool, 555))
    finally:
        ss.sync_account = orig

    assert r["systemic"] is False
    assert len(pool.execs) == 1, "изолированный протухший аккаунт должен быть помечен"


def test_iso_date_params_bound_as_text():
    """registered_estimate/last_seen_at приходят ISO-СТРОКАМИ.

    Голый ::date/::timestamptz заставлял asyncpg кодировать str как date/timestamp
    и звать .toordinal()/.timestamp() → падало на КАЖДОМ контакте, весь синк давал
    0. Приводим текст→тип в Postgres (::text::date / ::text::timestamptz). Тип-
    ошибки связывания заглушка пула не ловит, поэтому это структурный гейт.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "services" / "contacts_hub" / "sync_service.py").read_text(encoding="utf-8")
    body = src[src.index("async def sync_account("): src.index("async def sync_all_accounts(")]
    # Правильная форма присутствует…
    assert "$12::text::date" in body and "$14::text::timestamptz" in body, "INSERT: нет text-приведения"
    assert "$9::text::date" in body and "$11::text::timestamptz" in body, "UPDATE: нет text-приведения"
    # …и НЕТ старой формы, которая падала на str.
    import re
    for bad in (r"\$12::date\b", r"\$14::timestamptz\b", r"\$9::date\b", r"\$11::timestamptz\b"):
        assert not re.search(bad, body), f"осталась падающая привязка {bad}"


def test_sync_account_does_not_mark_itself():
    """Пометку acc_status теперь принимает агрегатор — sync_account не пишет БД
    в except (иначе системный сбой снова массово метил бы аккаунты)."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "services" / "contacts_hub" / "sync_service.py").read_text(encoding="utf-8")
    # тело sync_account — до начала sync_all_accounts
    body = src[src.index("async def sync_account("): src.index("async def sync_all_accounts(")]
    assert "acc_status='session_expired'" not in body, (
        "sync_account снова метит аккаунт сам — при системном сбое это массовая ложная тревога"
    )
