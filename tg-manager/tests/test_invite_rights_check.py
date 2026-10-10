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
    # Проверка должна срабатывать, когда поле группы теряет фокус. Сверять точную
    # строку onblur="checkInviteRights()" нельзя: в тот же обработчик с тех пор
    # добавили подсчёт аудитории и переливание, и тест краснел на добавлении
    # соседнего вызова, хотя проверка прав никуда не девалась.
    m = re.search(r'<input[^>]*id="massInviteGroup"[^>]*>', html)
    assert m, "нет поля группы для инвайта"
    ob = re.search(r'onblur="([^"]*)"', m.group(0))
    assert ob and "checkInviteRights()" in ob.group(1), (
        "проверка прав не вызывается при уходе из поля группы: "
        + (ob.group(1) if ob else "onblur нет вовсе"))
    # честное предупреждение о причине «вступят, но не смогут приглашать»
    assert "вступят, но не смогут приглашать" in html


def test_own_channel_account_is_checked_first():
    """Жалоба 05.10.2026: канал из своего списка — «система не видит ни одного
    аккаунта». Проверялись первые восемь аккаунтов по id; владельца канала среди
    них могло не быть, остальные ещё не вступили — и экран говорил «ни один не
    админ». Теперь первыми идут аккаунты, к которым канал привязан."""
    src = _src()
    for name in ("invite_rights_check", "invite_grant_admin"):
        m = re.search(rf"async def {name}\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
        assert m, name
        assert "_own_channel_acc_ids(uid, group)" in m.group(1), name
        assert "ORDER BY a.id LIMIT 8" not in m.group(1), name


def test_unchecked_is_not_reported_as_no_admin():
    html = _index()
    i = html.index("function checkInviteRights(")
    body = html[i:i + 4000]
    assert "!d.checked" in body, "«проверить было некому» сливалось с «админа нет»"
    j = html.index("async function openMassInviteForChannel(")
    assert "loadFleetReadiness()" in html[j:j + 2500], (
        "канал из своего списка — готовность флота должна проверяться сразу")
