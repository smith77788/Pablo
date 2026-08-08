"""Регрессия: mini-app эндпойнты team/audit НЕ должны утекать чужие данные.

Раньше /api/miniapp/team/members отдавал весь platform_users, а
/api/miniapp/audit — операции всех владельцев (JOIN без owner_id). Любой
авторизованный юзер видел данные всей платформы. Эти проверки фиксируют,
что оба запроса привязаны к uid запрашивающего.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _src() -> str:
    return inspect.getsource(mini_app_api)


def test_team_members_is_tenant_scoped():
    src = _src()
    m = re.search(r"async def team_members\(.*?\n(.*?)app\.router", src, re.DOTALL)
    assert m, "team_members handler not found"
    body = m.group(1)
    # Не должно быть выборки всех пользователей без фильтра
    assert "FROM platform_users pu" in body or "workspace_members" in body, (
        "team_members должен ограничивать выборку рабочими пространствами"
    )
    assert "$1" in body and "uid" in body, (
        "team_members должен фильтровать по uid запрашивающего (утечка данных)"
    )


def test_audit_trail_filters_by_owner():
    src = _src()
    m = re.search(r"async def audit_trail\(.*?\n(.*?)app\.router", src, re.DOTALL)
    assert m, "audit_trail handler not found"
    body = m.group(1)
    assert "oq.owner_id = $1" in body, (
        "audit_trail должен фильтровать по oq.owner_id (иначе видны чужие операции)"
    )
    assert "uid" in body


def test_ecosystem_overlaps_checks_ownership():
    src = _src()
    m = re.search(
        r"async def ecosystem_overlaps\(.*?\n(.*?)async def ", src, re.DOTALL
    )
    assert m, "ecosystem_overlaps handler not found"
    body = m.group(1)
    # eco_id из пути должен проверяться на владение перед чтением каналов
    assert "FROM ecosystems WHERE id=$1 AND owner_id=$2" in body, (
        "ecosystem_overlaps должен проверять владение eco_id (иначе утечка чужой экосистемы)"
    )
