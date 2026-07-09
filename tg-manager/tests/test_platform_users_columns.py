"""Регрессия (класс бага): запросы к platform_users по несуществующим колонкам.

platform_users НЕ имеет колонки created_at — реальное имя registered_at
(schema_v39). bot/handlers/admin.py в fallback-поиске владельца (когда ADMIN_IDS
пуст и нет сессионных админов) сортировал `ORDER BY created_at` → запрос падал
`column "created_at" does not exist`, и админ вообще не определялся. Исправлено
на COALESCE(registered_at, first_seen).

(admin_users/admin_user_detail в mini_app_api покрыты test_admin_users_columns.py.)
"""
from __future__ import annotations

import inspect
import re

from bot.handlers import admin as admin_mod
from services import mini_app_api


def test_mini_app_no_bare_platform_users_created_at_alias():
    """Ни один запрос в mini_app_api не должен обращаться к pu.created_at /
    pu.last_active_at на platform_users (алиас pu) — таких колонок нет
    (реальные registered_at/last_seen). Носитель бага — team_members (экран
    «Команда» отдавал 500). Допустим только явный алиас `... AS created_at`."""
    src = inspect.getsource(mini_app_api)
    bad = re.findall(r"pu\.(?:created_at|last_active_at)\b", src)
    assert not bad, (
        "platform_users (алиас pu) не имеет created_at/last_active_at — "
        f"используйте registered_at/last_seen с алиасом. Найдено: {bad}"
    )


def test_team_members_aliases_platform_users_columns():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def team_members\(.*?\n(.*?)app\.router", src, re.DOTALL)
    if not m:
        m = re.search(r"async def team_members\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "team_members handler not found"
    body = m.group(1)
    assert "registered_at AS created_at" in body, (
        "team_members должен алиасить registered_at AS created_at"
    )
    assert "last_seen AS last_active_at" in body, (
        "team_members должен алиасить last_seen AS last_active_at"
    )


def test_admin_fallback_owner_query_uses_real_column():
    src = inspect.getsource(admin_mod)
    # ни один запрос к platform_users в admin.py не должен сортировать/фильтровать
    # по несуществующей created_at
    for m in re.finditer(r"FROM platform_users[^\"]*", src):
        assert "created_at" not in m.group(0), (
            "platform_users не имеет created_at — используйте registered_at"
        )
    # конкретно fallback-поиск владельца (current_plan IS NOT NULL) должен
    # сортироваться по реальной registered_at
    fb = re.search(r"platform_users WHERE current_plan IS NOT NULL[\s\S]{0,120}", src)
    assert fb, "fallback-запрос владельца не найден"
    assert "registered_at" in fb.group(0), (
        "fallback-запрос владельца должен сортироваться по registered_at"
    )
