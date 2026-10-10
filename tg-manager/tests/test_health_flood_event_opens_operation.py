"""Флуд-событие на «Дэшборде здоровья» ведёт в ОПЕРАЦИЮ, а не в аккаунт.

Жалоба владельца: «если нажать на флуд-операцию из списка — переводит в
карточку аккаунта вместо самой операции с возможностью исправить причину
ошибки».

Причина флуда (какая операция флудила, её статус и текст ошибки) живёт в
карточке операции (openOpDetail → показывает «Причина ошибки» и разбор), а не
в карточке аккаунта. Раньше строка события всегда звала openAccount — тупик по
сути запроса. Фикс:
  • health_overview отдаёт operation_id + op_exists (через LEFT JOIN
    operation_queue, скоуп по владельцу) и человекочитаемое имя/статус операции;
  • строка события зовёт openOpDetail(operation_id), а openAccount оставляет
    фолбэком на случай, когда операции уже нет или флуд записан без неё.
"""
from __future__ import annotations

import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def _api() -> str:
    with open(API, encoding="utf-8") as f:
        return f.read()


def _health_overview_body() -> str:
    src = _api()
    m = re.search(r"async def health_overview\(.*?\n(.*?)\n    async def ",
                  src, re.DOTALL)
    assert m, "health_overview не найден"
    return m.group(1)


def test_backend_returns_operation_link_for_flood_events():
    body = _health_overview_body()
    assert "afl.operation_id" in body, (
        "событие не отдаёт operation_id — фронт не сможет открыть операцию")
    assert "op_exists" in body, (
        "нет флага op_exists — фронт не отличит живую операцию от вычищенной")
    # Связь с операцией и скоуп по владельцу (чужую операцию показывать нельзя).
    assert "LEFT JOIN operation_queue" in body
    assert "oq.owner_id = ta.owner_id" in body, "LEFT JOIN операций без owner-скоупа"


def _open_health_block() -> str:
    html = _html()
    i = html.index("async function openHealth(")
    j = html.index("txt('healthEvents', html);", i)
    return html[i:j]


def test_flood_event_row_opens_the_operation():
    block = _open_health_block()
    assert "openOpDetail(" in block, (
        "строка флуд-события не ведёт в карточку операции (openOpDetail)")
    # openAccount остаётся — но только как фолбэк, вместе с проверкой op_exists.
    assert "op_exists" in block and "operation_id" in block, (
        "переход в операцию не завязан на op_exists/operation_id")
    # Пауза показывается по-человечески, а не сырыми секундами «172800 с».
    assert "humanDur(ev.flood_seconds)" in block, (
        "пауза события не форматируется humanDur — остаются сырые секунды")
