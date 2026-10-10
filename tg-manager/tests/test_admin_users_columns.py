"""Регрессия: /api/miniapp/admin/users и /admin/user/{id} падали с
`column "created_at" does not exist`, потому что запрашивали несуществующие
колонки таблицы platform_users. Реальные колонки — registered_at и last_seen
(см. schema_v39.sql); фронт (mini_app/index.html) ожидает поля created_at /
last_active_at в JSON-ответе, поэтому колонки нужно алиасить, а не переименовывать
на фронте.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _src() -> str:
    return inspect.getsource(mini_app_api)


def test_admin_users_aliases_registered_at_as_created_at():
    src = _src()
    m = re.search(r"async def admin_users\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "admin_users handler not found"
    body = m.group(1)
    assert "registered_at AS created_at" in body, (
        "admin_users должен алиасить registered_at AS created_at "
        "(platform_users не имеет колонки created_at)"
    )
    assert "ORDER BY registered_at" in body, (
        "admin_users должен сортировать по существующей колонке registered_at"
    )


def test_admin_user_detail_aliases_registered_at_as_created_at():
    src = _src()
    m = re.search(r"async def admin_user_detail\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "admin_user_detail handler not found"
    body = m.group(1)
    assert "registered_at AS created_at" in body, (
        "admin_user_detail должен алиасить registered_at AS created_at "
        "(фронт читает u.created_at для отображения даты регистрации)"
    )
