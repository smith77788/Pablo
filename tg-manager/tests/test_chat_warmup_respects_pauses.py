"""Регрессия: прогрев чата обязан уважать паузу Telegram.

Три дыры, все в одну сторону — аккаунт продолжал действовать под действующим
ограничением.

1. Ход брал аккаунт, не глядя на его кулдаун и на общий пульс здоровья. Прогрев
   — такое же живое действие, как операция: ход под ограничением приближает
   спамблок, а не отдаляет его.
2. Вступление в чат стояло под голым `except Exception: pass`. Замысел был
   правильный — «уже участник», базовая группа или отсутствие прав читать и
   писать не мешают. Но тем же `pass` глоталась и ПАУЗА: дальше аккаунт читал
   историю, ставил реакцию и писал сообщение.
3. Любая ошибка хода уходила в debug-лог. Пауза Telegram нигде не
   учитывалась, и следующая подсистема брала этот аккаунт как спокойный.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from services import chat_warmup as cw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "services", "chat_warmup.py"), encoding="utf-8") as f:
        return f.read()


def _fn_body(name: str) -> str:
    src = _src()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


class _FloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


class _AlreadyParticipant(Exception):
    pass


class _Client:
    def __init__(self, join_exc=None):
        self.join_exc = join_exc
        self.calls: list[str] = []

    async def get_entity(self, ref):
        self.calls.append("get_entity")
        return object()

    async def __call__(self, request):
        self.calls.append("join")
        if self.join_exc is not None:
            raise self.join_exc
        return object()


# --- вступление не глотает паузу -----------------------------------------

def test_join_reraises_a_flood():
    client = _Client(join_exc=_FloodWaitError(300))
    with pytest.raises(Exception) as ei:
        asyncio.run(cw._resolve_and_join(client, "@chat"))
    assert "300" in str(ei.value), (
        "пауза Telegram проглочена — ход продолжится под ограничением"
    )


def test_join_reraises_the_chat_limit():
    client = _Client(join_exc=type("UserChannelsTooMuchError", (Exception,), {})())
    with pytest.raises(Exception):
        asyncio.run(cw._resolve_and_join(client, "@chat"))


def test_join_still_ignores_harmless_refusals():
    """Уже участник / нет прав — читать и писать всё равно можно."""
    for exc in (_AlreadyParticipant("already a participant"),
                ValueError("ChatAdminRequiredError")):
        client = _Client(join_exc=exc)
        entity = asyncio.run(cw._resolve_and_join(client, "@chat"))
        assert entity is not None, f"{exc!r}: ход отменён без причины"


# --- пауза во время хода попадает в пульс здоровья -----------------------

def test_turn_records_a_flood():
    body = _fn_body("_process_session")
    assert "record_flood" in body, (
        "пауза Telegram во время хода нигде не учитывается"
    )
    assert "flood_seconds" in body


# --- ход не берёт аккаунт на паузе ---------------------------------------

def test_turn_skips_a_cooling_account():
    body = _fn_body("_process_session")
    assert "cooldown_until" in body, (
        "ход берёт аккаунт, не глядя на его паузу"
    )


def test_turn_asks_the_health_pulse():
    body = _fn_body("_process_session")
    assert "is_account_quarantined" in body, (
        "ход не спрашивает единый пульс здоровья флота"
    )


def test_health_gate_is_fail_open():
    """Недоступный пульс не должен останавливать прогрев."""
    body = _fn_body("_process_session")
    i = body.index("is_account_quarantined")
    assert "except Exception" in body[i:i + 400], (
        "сбой пульса останавливает прогрев — это не fail-open"
    )
