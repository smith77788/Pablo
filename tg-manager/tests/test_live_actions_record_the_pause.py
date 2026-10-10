"""Регрессия: любое живое действие аккаунтом записывает паузу Telegram.

Форма дефекта одна и та же во всех подсистемах: паузу ловят, как-то на неё
реагируют локально (замедляют пейсинг, пропускают цикл, переносят доставку,
показывают текст оператору) — и не записывают.

Локальной реакции мало. Выбор аккаунта под следующее действие
(`flood_engine.get_best_account`, `resource_selector.select_account`) отсеивает
аккаунты по `tg_accounts.cooldown_until`. Пока пауза не записана, аккаунт для
всего остального продукта спокоен: его тут же берёт операция, разбор аудитории
или рассылка — и уводит под то же самое действующее ограничение, где следующая
пауза будет длиннее предыдущей.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

from services import flood_engine as fe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


class _SlowMode(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(
            f"A wait of {seconds} seconds is required before sending another "
            "message in this chat")


@pytest.fixture
def _recorded(monkeypatch):
    seen: list[tuple] = []

    async def _rec(pool, account_id, wait_seconds, action_type="default",
                   operation_id=None):
        seen.append((account_id, wait_seconds, action_type))
        return float(wait_seconds)

    monkeypatch.setattr(fe, "record_flood", _rec)
    return seen


# --- общий помощник -------------------------------------------------------

def test_note_flood_records_and_returns(_recorded):
    secs = asyncio.run(fe.note_flood(object(), 42, _FloodWaitError(300), "activity"))
    assert secs == 300
    assert _recorded == [(42, 300, "activity")]


def test_note_flood_ignores_other_errors(_recorded):
    assert asyncio.run(fe.note_flood(object(), 42, ValueError("нет доступа"))) == 0
    assert _recorded == []


def test_slow_mode_does_not_cool_the_account(_recorded):
    """Медленный режим — свойство чата; здоровый аккаунт из работы не выводим."""
    assert asyncio.run(fe.note_flood(object(), 42, _SlowMode(60))) == 0
    assert _recorded == [], (
        f"аккаунт отправлен на кулдаун из-за настройки чата: {_recorded!r}"
    )


def test_note_flood_survives_a_broken_database(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(fe, "record_flood", _boom)
    assert asyncio.run(fe.note_flood(object(), 42, _FloodWaitError(60))) == 0


def test_note_flood_without_account_id(_recorded):
    assert asyncio.run(fe.note_flood(object(), None, _FloodWaitError(60))) == 60
    assert _recorded == []


# --- каждая подсистема действительно зовёт запись -------------------------

_SITES = [
    ("services/activity_engine.py", "run_resource_activity_session"),
    ("services/ghost_engine.py", "_process_profile"),
    ("services/content_mesh.py", "_poll_source"),
    ("services/content_mesh.py", "_process_delivery"),
    ("services/story_manager.py", "post_story"),
]


def _fn_body(rel: str, name: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        src = f.read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{rel}: функция {name} не найдена")


@pytest.mark.parametrize("rel,fn", _SITES)
def test_flood_branch_records(rel, fn):
    body = _fn_body(rel, fn)
    assert "FloodWait" in body, f"{rel}::{fn}: ветки паузы нет — проверка устарела"
    assert "note_flood(" in body, (
        f"{rel}::{fn}: пауза Telegram обработана локально, но не записана"
    )


# --- истории больше не отвечают по-английски ------------------------------

def test_story_flood_message_is_russian():
    body = _fn_body("services/story_manager.py", "post_story")
    assert 'f"FloodWait {e.seconds}s"' not in body
    i = body.index("except FloodWaitError")
    branch = body[i:i + 900]
    msg = re.search(r'"(Telegram[^"]+)"', branch)
    assert msg, "в ветке паузы нет русского объяснения для владельца"


def test_story_callers_pass_the_pool():
    for rel in ("bot/handlers/accounts.py", "services/mini_app_api.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            src = f.read()
        # Только ВЫЗОВЫ. Без этого сюда попадал `async def account_post_story(`
        # — объявление ручки мини-аппа, у которой в скобках, конечно, нет пула.
        found = 0
        for m in re.finditer(r"story_manager\.post_story\(", src):
            i = m.end()
            depth = 1
            while i < len(src) and depth:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            args = src[m.end():i - 1]
            found += 1
            assert "pool=pool" in args, (
                f"{rel}: публикация истории без пула — пауза не доедет до БД"
            )
        assert found, f"{rel}: вызов публикации истории не найден"
