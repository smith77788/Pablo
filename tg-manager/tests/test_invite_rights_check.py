"""Регрессия: проверка прав в чате ДО запуска инвайта.

Главная причина «вступили, но не инвайтят» — среди аккаунтов нет админа чата с
правом «Назначать администраторов», некому раздать invite_users. Эндпоинт
/api/miniapp/invite/rights_check проверяет это ЖИВЬЁМ до старта и возвращает
has_promoter/has_inviter; мини-апп показывает баннер под полем группы.
"""
from __future__ import annotations

import inspect
import pathlib
import re
from pathlib import Path

from services import mini_app_api
from tests.miniapp_source import api_source, js_function


def _src() -> str:
    return api_source()


def _index() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_route_registered():
    assert 'app.router.add_get("/api/miniapp/invite/rights_check", invite_rights_check)' in _src()


def _rights_path(name: str) -> str:
    """Тело обработчика плюс помощник, который несёт выборку кандидатов.

    Выборка была скопирована в двух обработчиках и сведена в
    `_promoter_candidates`. Тест, привязанный к тексту ВНУТРИ обработчика,
    краснел на этом переносе, хотя поведение не поменялось, — поэтому
    проверяется путь целиком: обработчик и помощник, которого он зовёт.
    """
    from tests.miniapp_source import py_function

    body = py_function("services/mini_app_invite.py", name)
    assert "_promoter_candidates(" in body, (
        f"{name} больше не берёт кандидатов общим помощником: проверьте, "
        "откуда теперь приходит выборка, и перепривяжите проверку к ней")
    helper = py_function("services/mini_app_invite.py", "_promoter_candidates")
    # Скоуп владельца вынесен в общее условие рядом с помощником — он тоже
    # часть пути, и без него проверка «скоупится по владельцу» ищет текст там,
    # где его больше нет.
    whole = (pathlib.Path(__file__).resolve().parents[1]
             / "services" / "mini_app_invite.py").read_text(encoding="utf-8")
    m = re.search(r"_PROMOTER_WHERE = \((.*?)\)\n", whole, re.DOTALL)
    assert m, "общее условие выборки кандидатов не найдено"
    return body + "\n" + helper + "\n" + m.group(1)


def test_handler_checks_admin_status_and_scopes_owner():
    body = _rights_path("invite_rights_check")
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
    for name in ("invite_rights_check", "invite_grant_admin"):
        body = _rights_path(name)
        assert "_own_channel_acc_ids(uid, group)" in body, name
        assert "ORDER BY a.id LIMIT 8" not in body, name


def test_unchecked_is_not_reported_as_no_admin():
    html = _index()
    body = js_function("checkInviteRights")
    assert "!d.checked" in body, "«проверить было некому» сливалось с «админа нет»"
    j = html.index("async function openMassInviteForChannel(")
    assert "loadFleetReadiness()" in html[j:j + 2500], (
        "канал из своего списка — готовность флота должна проверяться сразу")
