"""Регрессия: проверка прав в чате ДО запуска инвайта.

Главная причина «вступили, но не инвайтят» — среди аккаунтов нет админа чата с
правом «Назначать администраторов», некому раздать invite_users. Эндпоинт
/api/miniapp/invite/rights_check проверяет это ЖИВЬЁМ до старта и возвращает
has_promoter/has_inviter; мини-апп показывает баннер под полем группы.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _src() -> str:
    return inspect.getsource(mini_app_api)


def _index() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_route_registered():
    assert 'app.router.add_get("/api/miniapp/invite/rights_check", invite_rights_check)' in _src()


def test_handler_checks_admin_status_and_scopes_owner():
    src = _src()
    m = re.search(r"async def invite_rights_check\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "invite_rights_check handler not found"
    body = m.group(1)
    assert "if not uid" in body and "401" in body, "нужна авторизация"
    assert "channel_admin_status" in body, "должен проверять права аккаунта в чате живьём"
    assert "owner_id=$1" in body, "должен скоупиться по владельцу (не чужие аккаунты)"
    assert "has_promoter" in body and "has_inviter" in body


def test_frontend_banner_wired():
    html = _index()
    assert "function checkInviteRights(" in html, "нет обработчика проверки прав"
    assert "/api/miniapp/invite/rights_check?group=" in html, "фронт не зовёт эндпоинт"
    assert 'onblur="checkInviteRights()"' in html, "проверка должна триггериться по вводу группы"
    # честное предупреждение о причине «вступят, но не смогут приглашать»
    assert "вступят, но не смогут приглашать" in html
