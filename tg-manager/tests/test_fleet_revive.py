"""Поднять флот: переподключённые аккаунты должны сразу работать.

Переимпорт/переподключение обновляли session_str и is_active, но НЕ снимали
операционные блокировки, унаследованные от сожжённой сессии (in_operation,
cooldown_until, обнулённый trust_score) → аккаунт молча исключался из операций
(«переподключил, а флот не работает»). Фикс: add_tg_account чистит блокировки
при ON CONFLICT + ручной эндпоинт «Поднять флот» для уже переподключённого флота.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_reimport_clears_operational_blockers():
    src = open(os.path.join(ROOT, "database", "db.py"), encoding="utf-8").read()
    i = src.index("async def add_tg_account")
    body = src[i:i + 2500]
    # ON CONFLICT-ветка переимпорта снимает ВСЕ блокировки прошлой сессии
    assert "in_operation=FALSE" in body
    assert "cooldown_until=NULL" in body
    assert "trust_score=GREATEST(COALESCE(tg_accounts.trust_score, 1.0), 1.0)" in body


def test_revive_endpoint_and_route_exist():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def accounts_revive" in api
    assert '"/api/miniapp/accounts/revive"' in api
    i = api.index("async def accounts_revive")
    body = api[i:i + 3200]
    # снимает блокировки только у аккаунтов С сессией, оптимистично активирует
    assert "session_str IS NOT NULL" in body
    assert "in_operation=FALSE" in body
    assert "cooldown_until=NULL" in body
    assert "release_accounts" in body   # снимаем и in-memory lock op_worker


def test_revive_button_wired_in_ui():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "onclick=\"reviveFleet()\"" in html
    assert "async function reviveFleet" in html
    assert "/api/miniapp/accounts/revive" in html
