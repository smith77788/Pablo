"""Регрессия по скриншотам: «аккаунты не работают, хотя активны и подключены».

Аккаунт может быть is_active (в UI «Активен»), но единый риск-пульс
(restriction_events / flood / trust / health_score) держит его в
карантине/риске — и массовые операции его ТИХО пропускают
(is_account_quarantined, fail-open). Пользователь видит «Активен», а
действия по нему не идут.

Фикс: список и деталь аккаунта мержат health_status из get_account_health,
а UI показывает РЕАЛЬНОЕ состояние («🛑 На паузе» / «⚠️ Под риском») вместо
ложного «Активен». Эти проверки фиксируют, что связь не оборвётся.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index_html() -> str:
    p = Path(__file__).resolve().parent.parent / "mini_app" / "index.html"
    return p.read_text(encoding="utf-8")


def test_accounts_list_merges_health_pulse():
    src = _api_src()
    m = re.search(r"async def accounts\(.*?\n(.*?)\n    async def account_detail",
                  src, re.DOTALL)
    assert m, "accounts handler не найден"
    body = m.group(1)
    assert "get_account_health" in body, "список аккаунтов должен мержить риск-пульс"
    assert 'health_status' in body, "каждая строка должна получать health_status"
    # owner-scoped: для admin межтенантного просмотра health не мержим
    assert "not admin" in body, "health мержится только для owner-скоупа (не admin)"


def test_account_detail_includes_health():
    src = _api_src()
    m = re.search(r"async def account_detail\(.*?\n(.*?)\n    async def accounts_export",
                  src, re.DOTALL)
    assert m, "account_detail handler не найден"
    body = m.group(1)
    assert '"health": health' in body, "деталь аккаунта должна возвращать health"
    assert "get_account_health" in body


def test_ui_shows_real_state_for_active_but_quarantined():
    html = _index_html()
    # список: «активный» аккаунт с карантином/риском больше не «Активен»
    assert "a.health_status==='quarantine'" in html, (
        "список должен показывать паузу для карантинного активного аккаунта"
    )
    assert "a.health_status==='at_risk'" in html, (
        "список должен показывать риск для at_risk активного аккаунта"
    )
    assert "🛑 На паузе" in html and "⚠️ Под риском" in html
    # деталь: баннер объясняет ПОЧЕМУ активный аккаунт не работает
    assert "healthBanner" in html
    assert "массовые операции его" in html


def test_quarantine_reflex_still_used_by_operations():
    """Пульс, который UI теперь показывает, реально управляет операциями —
    иначе бейдж «На паузе» не соответствовал бы поведению."""
    from services import infra_memory
    assert hasattr(infra_memory, "is_account_quarantined")
    assert hasattr(infra_memory, "get_account_health")
