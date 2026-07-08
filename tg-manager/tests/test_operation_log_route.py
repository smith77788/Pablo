"""Регрессия: мёртвая кнопка «📋 Лог» в деталях операции.

Фронт (openOpDetail в mini_app/index.html) зовёт
`/api/miniapp/operation/{id}/log` с `.catch(()=>({logs:[]}))`, но маршрут не
был зарегистрирован в mini_app_api.py — 404 молча проглатывался, секция лога
ВСЕГДА была пустой. Тест фиксирует: маршрут зарегистрирован, хендлер читает
operation_log и скоупит доступ по owner_id (иначе утечка чужого лога по id).
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def test_operation_log_route_registered():
    src = inspect.getsource(mini_app_api)
    assert re.search(
        r'add_get\(\s*["\']/api/miniapp/operation/\{op_id\}/log["\']\s*,\s*operation_log',
        src,
    ), "маршрут /api/miniapp/operation/{op_id}/log должен быть зарегистрирован"


def test_operation_log_handler_scopes_by_owner():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def operation_log\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "handler operation_log not found"
    body = m.group(1)
    # владение проверяется до чтения лога
    assert "operation_queue WHERE id=$1 AND owner_id=$2" in body, (
        "operation_log должен проверять владение операцией по owner_id"
    )
    assert "FROM operation_log WHERE op_id=$1" in body, (
        "operation_log должен читать строки лога по op_id"
    )
