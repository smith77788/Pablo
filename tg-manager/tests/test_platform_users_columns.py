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
