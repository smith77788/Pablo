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
    """Здоровье аккаунтов на дашборде admin-aware И из единого пульса.

    Owner-срез (жалоба владельца): здоровье берётся из ЕДИНОГО пульса
    (infra_memory.get_account_health) — флуд/спам-блок/ограничения отражаются
    мгновенно, а не ждут 30-мин цикла trust_engine (раньше дашборд усреднял
    только trust_score и светил «100%» на флоте во флуде).

    Платформенный срез админа пульс не покрывает (owner-scoped), поэтому там
    health-aware SQL по всей платформе — и он учитывает НЕ только trust, а
    мёртвый статус/кулдаун/флуд (durable-сигналы пульса)."""
    import re
    src = (Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    m = re.search(r"async def dashboard\(request.*?_res = await asyncio\.gather", src, re.DOTALL)
    assert m, "dashboard не найден"
    body = m.group(0)
    # owner-срез читает единый пульс, а не усреднённый trust
    assert "get_account_health" in body, "owner-здоровье не из единого пульса"
    assert re.search(r'_q\["pulse"\]\s*=\s*_get_health\(pool, uid\)', body), \
        "пульс не подключён к дашборду для owner-среза"
    # admin-вариант — по всей платформе (WHERE is_active=true без owner_id)
    assert "_health_sql" in body
    assert re.search(r'if _adm:\s*\n\s*_q\["acc_health"\]', body), "нет admin-ветки здоровья"
    assert re.search(r"FROM tg_accounts WHERE is_active=true", body), "нет платформенного варианта здоровья"
    # admin-здоровье health-aware, а не чистый trust: учитывает мёртвый статус,
    # кулдаун и флуд (durable-сигналы пульса)
    assert "flood_count_7d" in body and "cooldown_until" in body and "acc_status" in body, \
        "платформенное здоровье считает только trust — флуд/спам-блок игнорируются"
