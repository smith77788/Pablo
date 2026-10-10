"""Честное зеркало: причина «мягкого» провала операции доходит до пользователя.

Класс «рапорт без причины»: исполнитель возвращает {status:'failed', reason:'…'}
БЕЗ исключения → воркер писал только result (jsonb), но НЕ error_msg; а
operation_status читал error_msg и result->>'summary' — оба пусты (reason лежал в
result под ключом 'reason'). Пользователь видел «Ошибка» без объяснения (ровно
«Проверка 0/6» без причины).

Фикс: (1) воркер пишет error_msg из reason/summary при мягком провале; (2)
operation_status коалесцирует error_msg←reason и summary←reason (для старых операций);
(3) деталь операции показывает «Итог» (summary) и «Причина ошибки».
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import op_status, op_worker, mini_app_api


def test_worker_persists_error_msg_on_soft_failure():
    src = inspect.getsource(op_worker)
    # в ветке завершения операции error_msg пишется из reason/summary
    assert 'result.get("reason") or result.get("summary")' in src
    # UPDATE завершения выставляет error_msg (COALESCE, чтобы не затирать при done)
    assert "error_msg=COALESCE($4, error_msg)" in src


def test_operation_status_coalesces_reason():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def operation_status.*?FROM operation_queue WHERE id=\$1", src, re.DOTALL)
    assert m, "operation_status не найден"
    body = m.group(0)
    # Выражение причины стало общим для бота и мини-аппа и строже прежнего: к
    # error_msg и reason добавилась вторая колонка last_error — туда пишут пути
    # ОЖИДАНИЯ (флуд-пауза, повторная попытка, рестарт воркера), и раньше
    # мини-апп про них молчал. Проверяем, что экран берёт общее выражение и что
    # оно по-прежнему покрывает обе прежние половины.
    assert "sql_error_reason()} AS error_msg" in body
    reason_sql = op_status.sql_error_reason()
    assert "error_msg" in reason_sql and "result->>'reason'" in reason_sql
    assert "last_error" in reason_sql
    assert "COALESCE(result->>'summary', result->>'reason') AS summary" in body


def test_detail_shows_summary_and_error():
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"async function openOpDetail\(opId\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "openOpDetail не найден"
    body = m.group(1)
    # строка «Итог» из summary и строка причины ошибки
    assert "Итог" in body and "o.summary" in body
    assert "Причина ошибки" in body
