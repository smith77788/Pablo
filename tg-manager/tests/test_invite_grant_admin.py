"""Регрессия: авто-назначение аккаунтов админами чата в один тап.

Убирает ручной шаг «сделай каждый аккаунт админом в Telegram»: эндпоинт находит
промоутера (создатель/админ с add_admins), берёт channel_id и сабмитит операцию
promote_all_admins через operation_bus (прогресс/отмена/гейт по тарифу).
"""
from __future__ import annotations

import inspect
import pathlib
import re
from pathlib import Path

from services import mini_app_api, mass_inviter_engine
from tests.miniapp_source import api_source


def _src() -> str:
    return api_source()


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


def _grant_path() -> str:
    """Обработчик плюс то, что несёт выборку кандидатов.

    Выборка кандидатов в промоутеры была скопирована в двух обработчиках и
    сведена в `_promoter_candidates` с общим условием `_PROMOTER_WHERE`. Тест,
    привязанный к тексту ВНУТРИ обработчика, покраснел на переносе, хотя скоуп
    владельца никуда не девался, — проверяется путь целиком.
    """
    from tests.miniapp_source import py_function

    body = py_function("services/mini_app_invite.py", "invite_grant_admin")
    assert "_promoter_candidates(" in body, (
        "обработчик больше не берёт кандидатов общим помощником: проверьте, "
        "откуда теперь приходит выборка, и перепривяжите проверку к ней")
    helper = py_function("services/mini_app_invite.py", "_promoter_candidates")
    whole = (pathlib.Path(__file__).resolve().parents[1]
             / "services" / "mini_app_invite.py").read_text(encoding="utf-8")
    m = re.search(r"_PROMOTER_WHERE = \((.*?)\)\n", whole, re.DOTALL)
    assert m, "общее условие выборки кандидатов не найдено"
    return body + "\n" + helper + "\n" + m.group(1)


def test_grant_admin_submits_operation_via_bus():
    body = _grant_path()
    assert "if not uid" in body and "401" in body
    assert "owner_id=$1" in body, "скоуп по владельцу (не чужие аккаунты)"
    # Права аккаунтов проверяются общим параллельным сканом (_scan_admin_status),
    # он и зовёт channel_admin_status; важно, что промоутера ищут по can_promote.
    assert "_scan_admin_status" in body and "can_promote" in body, "должен искать промоутера"
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
