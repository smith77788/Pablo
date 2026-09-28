"""Регрессия: разбор сущности тоже записывает паузу Telegram.

Разбор канала или пользователя — такое же живое действие аккаунтом, как
массовая операция. Раньше любая ошибка здесь уходила в warning, а наружу
возвращалось None: для остального продукта аккаунт оставался спокойным, и
следующая подсистема брала его под уже действующее ограничение.

Записать паузу мешало ещё одно: обработчик ошибки видит только клиента и не
знает, каким аккаунтом работает — выбор аккаунта спрятан внутри `_get_client`.
Поэтому клиент теперь помнит свой account_id.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from services import entity_analyzer as ea
from services import flood_engine as fe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


@pytest.fixture
def _recorded(monkeypatch):
    seen: list[tuple] = []

    async def _rec(pool, account_id, wait_seconds, action_type="default",
                   operation_id=None):
        seen.append((account_id, wait_seconds, action_type))
        return float(wait_seconds)

    monkeypatch.setattr(fe, "record_flood", _rec)
    return seen


class _Client:
    def __init__(self, acc_id=None):
        if acc_id is not None:
            self._infragram_acc_id = acc_id


def test_pause_reaches_the_health_pulse(_recorded):
    asyncio.run(ea._note_flood(object(), _Client(77), _FloodWaitError(300)))
    assert _recorded == [(77, 300, "analyze")], (
        f"пауза разбора не дошла до пульса здоровья: {_recorded!r}"
    )


def test_other_errors_are_not_recorded(_recorded):
    asyncio.run(ea._note_flood(object(), _Client(77), ValueError("приватный канал")))
    assert _recorded == []


def test_client_without_account_id_does_not_crash(_recorded):
    asyncio.run(ea._note_flood(object(), _Client(), _FloodWaitError(60)))
    assert _recorded == []


def test_broken_pulse_does_not_break_the_analysis(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(fe, "record_flood", _boom)
    asyncio.run(ea._note_flood(object(), _Client(77), _FloodWaitError(60)))


def _fn_body(name: str) -> str:
    with open(os.path.join(ROOT, "services", "entity_analyzer.py"), encoding="utf-8") as f:
        src = f.read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def test_client_remembers_its_account():
    body = _fn_body("_get_client")
    assert "_infragram_acc_id" in body, (
        "клиент не помнит свой аккаунт — паузу некуда записать"
    )


@pytest.mark.parametrize("fn", ["analyze_channel", "analyze_user",
                                "analyze_telegram_object"])
def test_every_entry_point_records(fn):
    body = _fn_body(fn)
    assert "_note_flood(" in body, (
        f"{fn}: ошибка разбора глотается, пауза Telegram теряется"
    )


def test_record_happens_before_disconnect():
    """После disconnect у клиента уже не спросить, каким аккаунтом работали."""
    body = _fn_body("analyze_telegram_object")
    i_note = body.index("_note_flood(")
    i_disc = body.index("await client.disconnect()")
    assert i_note < i_disc, (
        "запись идёт после отключения клиента — аккаунт уже не определить"
    )
