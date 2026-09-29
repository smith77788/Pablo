"""Операция, поднятая сторожем, объясняет владельцу, почему она снова ждёт.

ЧТО ЛОМАЛОСЬ. Два сторожа возвращают операцию в очередь: один при старте
процесса (операция осталась в 'running' после падения), другой периодически
(операция висит в 'running' дольше отведённого времени). Оба писали ей
`status='pending'`, `done_items=0` и НИЧЕГО не писали в причину.

Для владельца это выглядело как потерянная работа: операция, которая шла и
показывала прогресс, вдруг откатывалась на ноль и снова «ожидала» — без единого
слова объяснения. Про соседние случаи ему пишут: флуд-пауза отправляет
уведомление, исчерпанный бюджет живучести оставляет текст в причине провала.
Здесь был пробел, причём именно там, где владелец скорее всего решит отменить
операцию и запустить её заново вручную — то есть сделает работу дважды.

ЧТО ТЕПЕРЬ. Оба пути пишут причину в `last_error`, и обе поверхности её
показывают (общее выражение op_status.sql_error_reason).
"""
from __future__ import annotations

import ast
import inspect

import pytest


def _func_src(name: str) -> str:
    from services import op_worker

    src = inspect.getsource(op_worker)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


class _RecordingPool:
    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return 0

    def revives(self) -> list[tuple]:
        return [c for c in self.calls
                if "status = 'pending'" in c[0] and "revive_count" in c[0]]


def test_the_texts_are_in_russian_and_explain_themselves():
    from services import op_worker

    for text in (op_worker._REVIVE_AFTER_RESTART,
                 op_worker._REVIVE_AFTER_SILENCE):
        assert "операц" in text.lower(), "владелец не понимает, о чём речь"
        # Владелец не читает по-английски — это условие проекта, не вкус.
        assert not any(ch.isascii() and ch.isalpha() for ch in text.replace("{mins}", "")), (
            f"в тексте для владельца осталась латиница: {text}"
        )


def test_the_silence_text_names_the_waiting_time():
    from services import op_worker

    text = op_worker._REVIVE_AFTER_SILENCE.format(
        mins=op_worker._STALE_RUNNING_TIMEOUT_MIN)

    assert str(op_worker._STALE_RUNNING_TIMEOUT_MIN) in text, (
        "«не отвечала долго» без числа владельцу ничего не говорит"
    )


@pytest.mark.asyncio
async def test_startup_revive_writes_the_reason():
    from services import op_worker

    pool = _RecordingPool()
    await op_worker._reset_stale_running(pool)

    revives = pool.revives()
    assert revives, "сброс зависших операций при старте не выполнен"
    query, args = revives[0]
    assert "last_error" in query, (
        "операция поднята молча — владелец увидит откат прогресса на ноль без "
        "объяснения и запустит работу заново вручную"
    )
    assert op_worker._REVIVE_AFTER_RESTART in args


@pytest.mark.asyncio
async def test_watchdog_revive_writes_the_reason():
    from services import op_worker

    pool = _RecordingPool()
    await op_worker._watchdog_stale(pool)

    revives = pool.revives()
    assert revives, "сторож зависших операций не выполнил сброс"
    query, args = revives[0]
    assert "last_error" in query, "сторож вернул операцию в очередь молча"
    assert any(str(op_worker._STALE_RUNNING_TIMEOUT_MIN) in str(a) for a in args)


@pytest.mark.asyncio
async def test_the_revive_still_resets_progress_and_counts_the_budget():
    """Причина добавлена, а не подменила смысл: счётчик и прогресс на месте."""
    from services import op_worker

    pool = _RecordingPool()
    await op_worker._watchdog_stale(pool)

    query, _ = pool.revives()[0]
    assert "done_items = 0" in query, "прогресс не сброшен — класс «done > total»"
    assert "revive_count = COALESCE(revive_count, 0) + 1" in query, (
        "бюджет живучести не расходуется — зависающая операция будет вечно "
        "забирать слот и аккаунты"
    )


def test_both_watchdogs_use_the_shared_texts():
    for name in ("_reset_stale_running", "_watchdog_stale"):
        src = _func_src(name)
        assert "_REVIVE_AFTER" in src, (
            f"{name} пишет причину своим текстом — два объяснения одного и того "
            f"же события разойдутся"
        )
