"""Регрессия: пауза Telegram в прогреве обязана попасть в пульс здоровья.

Прогрев умел паузу переждать ровно столько, сколько просит Telegram, и даже
остановить план при очень длинной. Но нигде её не записывал.

Для остального продукта аккаунт при этом оставался спокойным: выбор аккаунта
под операцию, разбор аудитории или рассылку брал его сразу же и уводил под то
же самое действующее ограничение. Следующая пауза от Telegram будет длиннее
предыдущей — так набирается спамблок. Прогрев запускается чаще всех остальных
подсистем, поэтому именно здесь это дороже всего.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from services import account_warmer as aw
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


def test_flood_is_recorded(_recorded):
    secs = asyncio.run(aw._note_flood(object(), 42, _FloodWaitError(300)))

    assert secs == 300
    assert _recorded == [(42, 300, "warmup")], (
        f"пауза не дошла до пульса здоровья: {_recorded!r}"
    )


def test_non_flood_is_not_recorded(_recorded):
    secs = asyncio.run(aw._note_flood(object(), 42, ValueError("канал недоступен")))

    assert secs == 0
    assert _recorded == []


def test_missing_account_id_does_not_crash(_recorded):
    """Без id писать некуда, но длительность паузы вызывающему всё равно нужна."""
    assert asyncio.run(aw._note_flood(object(), 0, _FloodWaitError(60))) == 60
    assert _recorded == []


def test_broken_flood_engine_does_not_break_warmup(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(fe, "record_flood", _boom)
    assert asyncio.run(aw._note_flood(object(), 42, _FloodWaitError(60))) == 0


def _fn_body(name: str) -> str:
    with open(os.path.join(ROOT, "services", "account_warmer.py"), encoding="utf-8") as f:
        src = f.read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


@pytest.mark.parametrize("fn", ["_run_daily_warmup_impl", "_run_warmup_session_impl"])
def test_every_flood_branch_records(fn):
    """Обе ветки FloodWait обязаны писать в пульс, а не только ждать."""
    body = _fn_body(fn)
    assert 'FloodWaitError' in body, f"{fn}: ветки FloodWait больше нет — проверка устарела"
    assert "_note_flood(" in body, (
        f"{fn}: пауза Telegram переждана, но нигде не записана"
    )


def test_record_happens_before_the_plan_is_paused():
    """Длинная пауза останавливает план — запись обязана случиться ДО выхода.

    Сравниваем внутри САМОЙ ветки FloodWait: `_pause_plan` встречается в
    функции и раньше, по другому поводу, и наивное сравнение первых вхождений
    сравнивало бы разные места.
    """
    body = _fn_body("_run_daily_warmup_impl")
    branch = body[body.index('if etype == "FloodWaitError":'):]
    i_note = branch.index("_note_flood(")
    i_pause = branch.index("_pause_plan(")
    assert i_note < i_pause, (
        "план останавливается раньше, чем пауза попадает в пульс — при длинном "
        "флуде запись не произойдёт вовсе"
    )
