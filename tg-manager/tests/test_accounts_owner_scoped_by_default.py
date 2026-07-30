"""Экран «Аккаунты» скоуплен по владельцу по умолчанию — даже у админа.

Баг (репорт пользователя): два разных админ-профиля видели ОДИН И ТОТ ЖЕ список
из 25 подключённых аккаунтов. Причина: `/api/miniapp/accounts` (и детально/экспорт/
«проверить все»/масс-операции/дашборд-счётчик) при `_is_admin(uid)` возвращали
МЕЖТЕНАНТНЫЙ срез (все аккаунты платформы, без owner-скоупа) — БЕЗ явного запроса.

Фикс: платформенный срез — только ЯВНЫЙ опт-ин (`?scope=platform` в query или
`{"scope":"platform"}` в теле) и только у админа. По умолчанию каждый профиль
(включая админа) видит СВОИ аккаунты.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from services import mini_app_api as M


class _Query(dict):
    pass


class _RelUrl:
    def __init__(self, q):
        self.query = _Query(q or {})


class _Req:
    def __init__(self, q=None):
        self.rel_url = _RelUrl(q)


def _run(coro):
    return asyncio.run(coro)


# ── Помощники скоупа ────────────────────────────────────────────────────────
def test_admin_without_scope_is_owner_scoped():
    with patch.object(M, "_is_admin", return_value=True):
        assert M._wants_platform_scope(_Req(), 111) is False
        assert M._wants_platform_scope(_Req({"scope": "platform"}), 111) is True
        assert M._wants_platform_scope(_Req({"scope": "all"}), 111) is True


def test_non_admin_never_gets_platform_scope():
    with patch.object(M, "_is_admin", return_value=False):
        assert M._wants_platform_scope(_Req({"scope": "platform"}), 111) is False


def test_body_scope_admin_only_and_explicit():
    with patch.object(M, "_is_admin", return_value=True):
        assert M._body_wants_platform_scope(111, {}) is False
        assert M._body_wants_platform_scope(111, {"scope": "platform"}) is True
    with patch.object(M, "_is_admin", return_value=False):
        assert M._body_wants_platform_scope(111, {"scope": "platform"}) is False


# ── Счётчик аккаунтов дашборда: owner-scoped когда не платформенный срез ─────
class _CapPool:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        # для счётчика аккаунтов вернём число, для остального 0
        return 0

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        return []


def _accounts_query(pool: "_CapPool") -> str:
    for q, _ in pool.queries:
        if "FROM tg_accounts" in q and "COUNT(*)" in q:
            return q
    raise AssertionError("нет запроса счётчика аккаунтов")


def test_stats_owner_scoped_when_not_platform():
    pool = _CapPool()
    _run(M._stats(pool, 777, admin=False))
    q = _accounts_query(pool)
    assert "owner_id=$1" in q, f"счётчик аккаунтов не owner-scoped: {q}"


def test_stats_platform_when_admin_opt_in():
    pool = _CapPool()
    _run(M._stats(pool, 777, admin=True))
    q = _accounts_query(pool)
    assert "owner_id" not in q, f"платформенный срез должен быть без owner_id: {q}"
