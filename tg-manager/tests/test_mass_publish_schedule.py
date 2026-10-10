"""Контент → публикация: расписание массовой публикации (end-to-end, с таймзоной).

Инфраструктура была готова (submit(scheduled_for), воркер уважает scheduled_for,
cancel работает на pending), но: (1) mass_publish не принимал scheduled_for;
(2) список операций НЕ возвращал scheduled_for — пользователь не видел и не мог
доверять, когда сработает запланированное.

Фикс: mass_publish принимает scheduled_for (UTC ISO, валидируется «в будущем»);
operations/operation_status возвращают scheduled_for; UI показывает «⏰ запланировано
на …» + отмену. Таймзона: datetime-local (локальное) → toISOString() (UTC) → бэк
fromisoformat (tz-aware) → воркер <= now() → показ обратно в локальном.
"""
from __future__ import annotations

import ast
import inspect
import re

from services import mini_app_api

SRC = inspect.getsource(mini_app_api)


def _fn(name, after=None):
    """Тело обработчика по границам из AST.

    Раньше срез шёл «от `name` до `async def <after>`», и соседний хендлер был
    частью контракта теста: когда `proxies` уехал в services/mini_app_proxies.py,
    регулярка перестала совпадать и тест упал на ВЕРНОМ коде («mass_publish не
    найден»). Границы функции знает только парсер — спрашиваем его.
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SRC, node) or ""
    raise AssertionError(f"{name} не найден")


def test_mass_publish_accepts_and_validates_schedule():
    body = _fn("mass_publish")
    assert 'body.get("scheduled_for")' in body, "должен принимать scheduled_for"
    # разбор ISO с учётом Z и tz-aware сравнение
    assert "fromisoformat" in body and 'replace("Z", "+00:00")' in body
    assert "должно быть в будущем" in body, "должен отклонять прошлое время"
    # прокидывает в submit
    assert "scheduled_for=scheduled_for" in body


def test_operations_list_returns_scheduled_for():
    assert "oq.scheduled_for" in SRC, "список операций должен отдавать scheduled_for"


def test_operation_status_returns_scheduled_for():
    body = _fn("operation_status")
    assert "scheduled_for" in body
    # ISO-конвертация тоже покрывает scheduled_for
    assert '("created_at", "finished_at", "scheduled_for")' in body


def test_ui_schedule_wired_with_utc_and_visibility():
    from pathlib import Path
    html = (mini_app_api and Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'id="mpSchedule"' in html, "нет поля расписания"
    m = re.search(r"async function sendMassPublish\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    body = m.group(1)
    # локальное → UTC ISO; валидация «в будущем»; отправка
    assert "toISOString()" in body
    assert "в будущем" in body
    assert "payload.scheduled_for = scheduled_for" in body
    # видимость: очередь показывает запланированное и даёт отмену
    assert "запланировано на" in html
    assert "scheduled_for&&new Date(o.scheduled_for)>new Date()" in html
