"""Регресс: отложенный пост ставится через operation_bus, а не сырым INSERT.

`schedule_post` делал прямой `INSERT INTO operation_queue` и биндил
`scheduled_at` (строку из JSON) в колонку `scheduled_for` (TIMESTAMPTZ) БЕЗ
`::timestamptz`. asyncpg на строку в timestamptz кидает DataError → эндпойнт
падал 500 при ЛЮБОМ вызове (тот же класс, что убил CRM-напоминание).

Миграция на `operation_bus.submit` чинит это разом: шина кастует
(`$5::timestamptz`, Postgres сам разбирает ISO) и применяет проверки плана/лимитов.
Заодно на один обход шины меньше — ratchet BASELINE опущен 35 → 34.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"


def _handler_src() -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def schedule_post.*?(?=\n    # ──|\n    async def )", src, re.DOTALL)
    assert m, "schedule_post не найден"
    return m.group(0)


def test_uses_operation_bus_not_raw_insert():
    h = _handler_src()
    assert "operation_bus.submit" in h, "операции ставятся через шину, а не сырым INSERT"
    assert "INSERT INTO operation_queue" not in h, (
        "прямой INSERT биндил строку в timestamptz без каста → DataError/500"
    )


def test_passes_scheduled_for_to_bus():
    h = _handler_src()
    assert re.search(r"scheduled_for\s*=\s*scheduled_at", h), (
        "время должно уходить в шину, которая кастует его к timestamptz"
    )


def test_returns_op_id_for_tracking():
    h = _handler_src()
    assert "op_id" in h, "клиент должен получить id операции, чтобы следить за статусом"


def test_ratchet_baseline_lowered():
    src = (ROOT / "tests" / "test_operation_bus_ratchet.py").read_text(encoding="utf-8")
    m = re.search(r"BASELINE\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) <= 34, (
        "после миграции обхода baseline обязан быть опущен (двигать вниз можно)"
    )
