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
