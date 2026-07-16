"""Регрессия: единичные операции возвращают РЕАЛЬНЫЙ итог, а не «поставлено в очередь».

Раньше единичные действия (вступить в канал, изменить канал, назначить админов,
действие с аккаунтом, проверка аккаунта, Ad Intelligence) делали enqueue и
показывали только «⏳ Поставлено в очередь». Если фоновая операция тихо падала
(мёртвый аккаунт/прокси), пользователь видел «ничего не происходит».

Добавлен GET /api/miniapp/operation/{op_id} + JS-хелпер pollOpResult, который
опрашивает статус и показывает итог. Эти проверки фиксируют, что:
  1. эндпойнт существует, требует авторизацию и скоупится по owner_id (не утечка);
  2. эндпойнт зарегистрирован в роутере;
  3. каждый единичный enqueue-колбэк подключает pollOpResult.
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


def test_operation_status_handler_owner_scoped():
    src = _api_src()
    m = re.search(r"async def operation_status\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "operation_status handler not found"
    body = m.group(1)
    # Требует авторизацию
    assert "if not uid" in body and "401" in body, (
        "operation_status должен возвращать 401 без uid"
    )
    # Скоупится по owner_id — иначе утечка чужих операций
    assert "WHERE id=$1 AND owner_id=$2" in body, (
        "operation_status должен фильтровать по owner_id (утечка чужих операций)"
    )
    # 404 на чужую/несуществующую операцию, а не тихий None
    assert "404" in body


def test_operation_status_route_registered():
    src = _api_src()
    assert 'app.router.add_get("/api/miniapp/operation/{op_id}", operation_status)' in src, (
        "GET /api/miniapp/operation/{op_id} должен быть зарегистрирован"
    )


def test_poll_op_result_helper_exists():
    html = _index_html()
    assert "async function pollOpResult(" in html, "JS-хелпер pollOpResult отсутствует"
    # хелпер должен опрашивать именно новый эндпойнт
    assert "/api/miniapp/operation/'+opId" in html or "/api/miniapp/operation/'+ opId" in html, (
        "pollOpResult должен опрашивать /api/miniapp/operation/<id>"
    )


def test_operation_status_returns_timestamps():
    """Детали операции показывают Создано/Завершено — SELECT обязан тянуть
    created_at и finished_at и отдавать их в ISO (иначе поля пустые в UI)."""
    src = _api_src()
    m = re.search(r"async def operation_status\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "operation_status handler not found"
    body = m.group(1)
    assert "created_at" in body and "finished_at" in body, (
        "operation_status должен возвращать created_at/finished_at для деталей операции"
    )
    # ISO-конвертация для фронта (new Date(...) не парсит datetime без isoformat)
    assert "isoformat" in body, "timestamps должны конвертироваться в ISO"


def test_open_op_detail_fetches_by_id():
    """Регрессия «Операция #N не найдена»: openOpDetail должен брать операцию ПО
    ID (любой статус), а не сканировать только список running — иначе
    завершённые/упавшие операции показывали «не найдена»."""
    html = _index_html()
    m = re.search(
        r"async function openOpDetail\(.*?\n(.*?)\nasync function ", html, re.DOTALL
    )
    assert m, "openOpDetail не найдена"
    body = m.group(1)
    assert "/api/miniapp/operation/'+opId" in body, (
        "openOpDetail должна запрашивать операцию по id, а не список"
    )
    assert "status=running&limit=500" not in body, (
        "openOpDetail не должна сканировать только running (баг «не найдена»)"
    )


def test_build_drawer_dedupes_nav():
    """Регрессия «много дублей / несколько дашбордов» в меню: buildDrawer не
    выводит повторно верхние вкладки, дедупит ярлыки и пропускает «Быстрые
    действия» (дубль навигации), не плодит пустые категории."""
    html = _index_html()
    m = re.search(
        r"function buildDrawer\(\)\s*\{(.*?)\n\}", html, re.DOTALL
    )
    assert m, "buildDrawer не найдена"
    body = m.group(1)
    # верхние вкладки засеяны в set, чтобы каталог их не повторял
    assert "const seen = new Set(" in body and "'дашборд'" in body, (
        "buildDrawer должна засеивать seen верхними вкладками"
    )
    assert "seen.has(key)" in body, "buildDrawer должна дедупить плитки по ярлыку"
    assert "быстрые действия" in body, (
        "buildDrawer должна пропускать секцию «Быстрые действия» (дубль навигации)"
    )


def test_single_item_callers_wire_poll_op_result():
    html = _index_html()
    # Каждый единичный enqueue-колбэк обязан подключить pollOpResult, иначе тихий провал
    for fn in (
        "submitQuickChannel",   # вступление в канал
        "submitChanEdit",       # изменение канала
        "promoteChannel",       # назначение админов
        "accAction",            # действие с аккаунтом
        "checkAccount",         # проверка аккаунта
        "submitAdIntelChannel", # ad intelligence
    ):
        m = re.search(
            r"function " + fn + r"\s*\(.*?\n(.*?)\n(?:async function|function) ",
            html,
            re.DOTALL,
        )
        assert m, f"{fn} не найдена в index.html"
        body = m.group(1)
        assert "pollOpResult(" in body, (
            f"{fn} должна вызывать pollOpResult для показа реального итога операции"
        )
