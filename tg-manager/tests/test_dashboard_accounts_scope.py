"""Дашборд: счётчик аккаунтов совпадает по скоупу с экраном «Аккаунты».

Баг (со скриншота): на «Главной» — «Аккаунтов: 0», на экране «Аккаунты» — «25».
Причина: экран аккаунтов для админа межтенантный (все аккаунты платформы), а
_stats дашборда всегда owner-scoped (админ владеет 0 → показывает 0). Фикс: _stats
принимает admin и для админа считает аккаунты платформы (совпадает с экраном), для
обычного пользователя — свои; счётчик — ВСЕГО (как крупная «25 аккаунтов» в шапке).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from services import mini_app_api


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakePool:
    """Ловит SQL и возвращает 25 для запроса аккаунтов, 0 для остального."""
    def __init__(self):
        self.acc_sql = None

    async def fetchval(self, sql, *args):
        if "FROM tg_accounts" in sql:
            self.acc_sql = sql
            return 25
        return 0

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None


def test_admin_counts_all_platform_accounts():
    pool = FakePool()
    res = _run(mini_app_api._stats(pool, uid=999, admin=True))
    assert res["accounts"] == 25
    # запрос аккаунтов админа — БЕЗ owner-фильтра (межтенантно, как экран «Аккаунты»)
    assert "owner_id" not in pool.acc_sql, pool.acc_sql


def test_non_admin_scopes_to_owner():
    pool = FakePool()
    res = _run(mini_app_api._stats(pool, uid=42, admin=False))
    assert res["accounts"] == 25
    # обычный пользователь — только свои
    assert "owner_id=$1" in pool.acc_sql, pool.acc_sql


def test_counts_total_not_only_active():
    # счётчик — ВСЕГО (совпадает с «25 аккаунтов» в шапке), фильтра is_active быть не должно
    pool = FakePool()
    _run(mini_app_api._stats(pool, uid=1, admin=True))
    assert "is_active" not in pool.acc_sql, pool.acc_sql


def test_dashboard_acc_health_admin_aware():
    """Здоровье аккаунтов на дашборде тоже admin-aware: для админа — по всей
    платформе (без owner_id), иначе показывал бы 100% при 0 своих аккаунтов."""
    import re
    src = (Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    m = re.search(r"async def dashboard\(request.*?_res = await asyncio\.gather", src, re.DOTALL)
    assert m, "dashboard не найден"
    body = m.group(0)
    # есть admin-ветка запроса здоровья без owner_id
    assert "_health_sql" in body
    assert "_adm else" in body
    # admin-вариант считает по всей платформе (WHERE is_active=true без owner_id)
    assert re.search(r"FROM tg_accounts WHERE is_active=true", body), "нет платформенного варианта здоровья"
