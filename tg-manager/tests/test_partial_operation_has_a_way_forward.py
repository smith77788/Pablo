"""Недоведённая операция всегда предлагает продолжить.

ЧТО БЫЛО. Итоговое сообщение об операции давало кнопку повтора только при
`failed > 0`. Но недоведённая операция может иметь НОЛЬ упавших целей: она
остановилась до того, как дошла до остальных — кончились аккаунты, пришла
флуд-пауза, исчерпан лимит. Тогда у неё 203 успеха, ноль ошибок и 177
нетронутых целей, а владелец читал «⚠️ частично выполнена» и НИ ОДНОЙ кнопки:
продолжить можно было только найдя операцию через меню.

Вторая половина того же: `operation_bus.resubmit_one` отказывал такой операции
словами «Неудавшихся целей не осталось». Отсутствие упавших целей — не повод
отказать: продолжить нужно целиком, и это безопасно, потому что уже взятые цели
исполнитель пропустит (дедуп инвайта по (owner_id, group_key), журнал целей по
операции).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from services import op_status, operation_bus

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Pool:
    def __init__(self, status=op_status.PARTIAL, op_type="mass_invite"):
        self.row = {"id": 31, "owner_id": 555, "op_type": op_type,
                    "status": status, "params": {"targets": ["a"]},
                    "label": "Приглашение", "total_items": 380,
                    "done_items": 203}
        self.writes: list[str] = []

    async def fetchrow(self, query, *args):
        return dict(self.row) if "operation_queue" in query else None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.writes.append(query)
        return "UPDATE 1"


def test_continuation_is_offered_when_nothing_failed(monkeypatch):
    """Ноль упавших целей — не повод отказать: продолжаем операцию целиком."""
    submitted: list[str] = []

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        submitted.append(op_type)
        return 777

    async def _nothing_to_retry(p, owner_id, src_op_id):
        return {"ok": False, "op_id": None, "count": 0,
                "reason": "Неудавшихся целей не осталось"}

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    monkeypatch.setattr(operation_bus, "submit_retry_failed", _nothing_to_retry)
    monkeypatch.setattr(operation_bus, "retry_targets_meta",
                        lambda op_type: {"param": "targets"})

    res = asyncio.run(operation_bus.resubmit_one(_Pool(), 555, 31))
    assert res["ok"] and res["op_id"] == 777, (
        "недоведённая операция с нулём упавших целей осталась без продолжения: "
        f"{res}")
    assert submitted == ["mass_invite"]


def test_pointwise_retry_still_wins_when_targets_failed(monkeypatch):
    """Обратная сторона: есть упавшие цели — повторяем только их."""
    submitted: list[str] = []

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        submitted.append(op_type)
        return 1

    async def _has_failed(p, owner_id, src_op_id):
        return {"ok": True, "op_id": 888, "count": 17, "reason": ""}

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    monkeypatch.setattr(operation_bus, "submit_retry_failed", _has_failed)
    monkeypatch.setattr(operation_bus, "retry_targets_meta",
                        lambda op_type: {"param": "targets"})

    res = asyncio.run(operation_bus.resubmit_one(_Pool(), 555, 31))
    assert res["op_id"] == 888 and submitted == [], (
        "повтор целиком подменил точечный повтор по упавшим целям")


def _notification_block() -> str:
    """Исходник функции, собирающей итоговое сообщение владельцу."""
    src = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_op_task":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("в op_worker.py нет функции _run_op_task")


def test_final_message_offers_a_way_out_of_partial():
    """Храповик: экран-тупик у недоведённой операции не возвращается.

    Тот же принцип, что у `test_no_dead_end_screens`: итог без единой кнопки —
    это выход только через меню, то есть владелец ищет свою же операцию руками.
    """
    body = _notification_block()
    assert "elif _final_status == op_status.PARTIAL:" in body, (
        "у недоведённой операции с нулём упавших целей снова нет кнопки "
        "продолжения")
    i = body.index("elif _final_status == op_status.PARTIAL:")
    branch = body[i:body.index("kb.button(\n                text=\"📋 Детали операции\"", i)]
    assert 'action="retry_op"' in branch, (
        "кнопка продолжения не ведёт в повтор операции")
    assert "Продолжить" in branch, "кнопка не говорит владельцу, что она делает"
