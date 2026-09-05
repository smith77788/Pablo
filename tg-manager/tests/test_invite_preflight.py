"""Предполётная проверка инвайта: флот, прокси, оценка проходов — ДО запуска.

Запрос пользователя: показать перед стартом, сколько аккаунтов реально пригодны
(с прокси / без прокси / с битым прокси / на отдыхе) и во сколько проходов уйдёт
аудитория. Плюс явно: работа БЕЗ прокси разрешена, но с предупреждением.

Ключевое разделение (честность по анти-детекту):
  • без прокси (direct) — РАЗРЕШЕНО (каноничное прямое соединение), но с
    предупреждением о риске бана/AUTH_KEY_DUPLICATED;
  • назначен прокси, но он недоступен (proxy_broken) — НЕ пригоден: прямое
    подключение убьёт сессию (AUTH_KEY_DUPLICATED), такие в инвайт не идут.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _preflight_body() -> str:
    src = _api_src()
    m = re.search(r"async def invite_preflight\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "invite_preflight не найден"
    return m.group(1)


def test_route_registered_and_authed():
    src = _api_src()
    assert 'add_get("/api/miniapp/invite/preflight", invite_preflight)' in src
    body = _preflight_body()
    assert "if not uid" in body and "401" in body, "эндпойнт требует авторизацию"


def test_classifies_fleet_by_transport():
    body = _preflight_body()
    # разбивка: с рабочим прокси / без прокси (direct) / с битым прокси
    assert "has_proxy" in body and "proxy_broken" in body, (
        "нужна классификация флота по транспорту"
    )
    # proxy_broken = назначен прокси (proxy_id) но он неактивен (p.id IS NULL)
    assert "a.proxy_id IS NOT NULL AND p.id IS NULL" in body
    # direct = вообще без назначенного прокси
    assert "proxy_id IS NOT NULL AND p.id IS NOT NULL" in body


def test_direct_accounts_allowed_with_warning_not_blocked():
    body = _preflight_body()
    # аккаунты без прокси попадают в usable (не исключаются) — разрешены
    assert "not r[\"proxy_broken\"]" in body or "not r['proxy_broken']" in body, (
        "из пригодных исключаем только битый-прокси, но НЕ direct — работа без прокси разрешена"
    )
    # и про них есть предупреждение
    assert "без прокси" in body and "разрешено" in body, (
        "работа без прокси должна быть явно разрешена с предупреждением"
    )


def test_broken_proxy_accounts_excluded_from_usable():
    body = _preflight_body()
    # битый прокси = не пригоден (прямое соединение убьёт сессию)
    assert "AUTH_KEY_DUPLICATED" in body, (
        "нужно объяснить, почему битый прокси нельзя гнать напрямую"
    )


def test_estimates_passes_from_daily_budget():
    body = _preflight_body()
    assert "recommended_daily_limit" in body, (
        "оценка проходов должна опираться на реальный суточный лимит флота"
    )
    assert "est_passes" in body and "per_pass" in body
    assert "one_pass" in body, "нужен явный признак «уйдёт за один проход»"


def test_respects_quarantine():
    body = _preflight_body()
    assert "is_account_quarantined" in body, (
        "карантинные аккаунты не должны считаться пригодными"
    )


def test_ui_calls_preflight_and_has_panel():
    from pathlib import Path
    from tests.miniapp_source import miniapp_source
    html = miniapp_source()
    assert 'id="massInvitePreflight"' in html, "нет панели предполётной проверки"
    assert "loadInvitePreflight(" in html and "invite/preflight" in html, (
        "UI должен запрашивать предполётную проверку перед запуском"
    )
