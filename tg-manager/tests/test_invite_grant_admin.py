"""Регрессия: авто-назначение аккаунтов админами чата в один тап.

Убирает ручной шаг «сделай каждый аккаунт админом в Telegram»: эндпоинт находит
промоутера (создатель/админ с add_admins), берёт channel_id и сабмитит операцию
promote_all_admins через operation_bus (прогресс/отмена/гейт по тарифу).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api, mass_inviter_engine


def _src() -> str:
    return inspect.getsource(mini_app_api)


def _index() -> str:
    from tests.miniapp_source import miniapp_source
    return miniapp_source()


def test_channel_admin_status_returns_channel_id():
    src = inspect.getsource(mass_inviter_engine.channel_admin_status)
    assert '"channel_id"' in src, (
        "channel_admin_status должен возвращать channel_id — по нему вызывается "
        "promote_all_admins"
    )


def test_grant_admin_route_registered():
    assert 'app.router.add_post("/api/miniapp/invite/grant_admin", invite_grant_admin)' in _src()


def test_grant_admin_submits_operation_via_bus():
    src = _src()
    m = re.search(r"async def invite_grant_admin\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "invite_grant_admin handler not found"
    body = m.group(1)
    assert "if not uid" in body and "401" in body
    assert "owner_id=$1" in body, "скоуп по владельцу (не чужие аккаунты)"
    assert "channel_admin_status" in body and "can_promote" in body, "должен искать промоутера"
    assert 'operation_bus' in body and '"promote_all_admins"' in body, (
        "должен сабмитить операцию через operation_bus, а не инлайн"
    )
    assert "no_promoter" in body, "нет промоутера → честный ответ, а не 500"
    assert "PermissionError" in body, "отказ по тарифу → 403, а не 500"


def test_frontend_grant_button_wired():
    html = _index()
    assert "async function massInviteGrantAdmin(" in html
    assert "/api/miniapp/invite/grant_admin" in html
    assert "Выдать право приглашать всем" in html
