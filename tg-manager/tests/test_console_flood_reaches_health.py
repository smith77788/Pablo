"""Регрессия: пауза Telegram из живой консоли доходит до пульса здоровья.

Живая консоль — ручное управление своим аккаунтом (диалоги, история, отправка).
Ходит она в Telegram ТЕМ ЖЕ аккаунтом, что и массовые операции.

Раньше консоль ловила FloodWait, показывала его владельцу по-русски и на этом
забывала. Для остального продукта аккаунт оставался «спокойным»: выбор
аккаунта под следующую операцию брал его снова и уводил под уже действующее
ограничение Telegram, где следующая пауза будет длиннее предыдущей. Это прямая
дорога к спамблоку.

Отдельно проверяем, что медленный режим чата на аккаунт кулдаун НЕ ставит:
это свойство чата, а не аккаунта, и выводить из работы здоровый аккаунт из-за
чужой настройки нельзя.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from services import account_console as ac
from services import flood_engine as fe


class _FloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required (caused by SendMessageRequest)")


class _SlowModeWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(
            f"A wait of {seconds} seconds is required before sending another message in this chat"
        )


@pytest.fixture
def _recorded(monkeypatch):
    seen: list[tuple] = []

    async def _rec(pool, account_id, wait_seconds, action_type="default",
                   operation_id=None):
        seen.append((account_id, wait_seconds, action_type))
        return float(wait_seconds)

    monkeypatch.setattr(fe, "record_flood", _rec)
    return seen


# --- общий разбор паузы ---------------------------------------------------

def test_one_flood_parser_for_the_whole_product():
    """Разбор паузы Telegram должен быть один, иначе он снова разойдётся."""
    from services import parser

    exc = _FloodWaitError(300)
    assert fe.flood_seconds(exc) == 300
    assert parser.flood_seconds(exc) == 300


def test_flood_parser_reads_the_real_telethon_text():
    class _NoAttr(Exception):
        pass

    exc = _NoAttr("A wait of 300 seconds is required")
    assert fe.flood_seconds(exc) == 300


def test_flood_parser_ignores_other_errors():
    assert fe.flood_seconds(ValueError("channel invalid")) is None


# --- консоль записывает паузу --------------------------------------------

def test_console_records_a_flood(_recorded):
    code, human = asyncio.run(ac.note_error(object(), {"id": 42}, _FloodWaitError(300)))

    assert code == "flood"
    assert _recorded == [(42, 300, "console")], (
        f"пауза Telegram не дошла до пульса здоровья: {_recorded!r}"
    )


def test_console_does_not_cool_down_on_slow_mode(_recorded):
    """Медленный режим — свойство чата; аккаунт из работы выводить нельзя."""
    code, _ = asyncio.run(ac.note_error(object(), {"id": 42}, _SlowModeWaitError(60)))

    assert code == "slow_mode"
    assert _recorded == [], (
        f"здоровый аккаунт отправлен на кулдаун из-за настройки чата: {_recorded!r}"
    )


def test_console_still_speaks_russian(_recorded):
    _, human = asyncio.run(ac.note_error(object(), {"id": 42}, _FloodWaitError(300)))
    assert not re.search(r"[A-Za-z]{4,}", human.replace("Telegram", "")), human


def test_console_survives_a_broken_flood_engine(monkeypatch):
    """Сбой записи не должен съесть ответ владельцу."""
    async def _boom(*a, **kw):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(fe, "record_flood", _boom)
    code, human = asyncio.run(ac.note_error(object(), {"id": 42}, _FloodWaitError(300)))
    assert code == "flood" and human


def test_console_without_account_id_does_not_crash(_recorded):
    code, human = asyncio.run(ac.note_error(None, None, _FloodWaitError(300)))
    assert code == "flood" and human and _recorded == []


# --- проводка: все сетевые функции консоли принимают и передают пул -------

_SESSION_FNS = ("list_dialogs", "get_history", "send_text", "send_file", "list_contacts")


@pytest.mark.parametrize("fn", _SESSION_FNS)
def test_session_function_accepts_a_pool(fn):
    sig = inspect.signature(getattr(ac, fn))
    assert "pool" in sig.parameters, (
        f"{fn}: нечем записать паузу Telegram — функция не получает пул"
    )


@pytest.mark.parametrize("fn", _SESSION_FNS)
def test_session_function_records_the_flood(fn):
    """Каждая сетевая функция консоли обязана звать общий обработчик ошибки."""
    import ast as _ast
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "services", "account_console.py")
    src = open(path, encoding="utf-8").read()
    lines = src.split("\n")
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.AsyncFunctionDef) and node.name == fn:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "note_error(pool, acc, exc)" in body, (
                f"{fn}: ошибка классифицируется мимо общего обработчика — "
                "пауза Telegram снова потеряется"
            )
            return
    pytest.fail(f"функция {fn} не найдена")


def test_miniapp_passes_the_pool():
    """Вызовы из мини-аппа без пула — запись паузы не доедет до БД."""
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "services", "mini_app_api.py")
    src = open(path, encoding="utf-8").read()
    calls = 0
    for m in re.finditer(r"account_console\.(\w+)\(", src):
        fn = m.group(1)
        if fn not in _SESSION_FNS:
            continue
        i = m.end()
        depth = 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        args = src[m.end():i - 1]
        assert "pool=pool" in args, f"{fn}: вызов без пула — {args[:80]}"
        calls += 1
    assert calls == len(_SESSION_FNS), (
        f"проверено {calls} вызовов из {len(_SESSION_FNS)} — проверка неполная"
    )
