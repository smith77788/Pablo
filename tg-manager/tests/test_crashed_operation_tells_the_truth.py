"""Операция, оборвавшаяся исключением, честно говорит, сколько успела сделать.

Успешный путь `_run_op_task` давно решает исход по РЕАЛЬНЫМ счётчикам
(`op_status.classify_final`), а путь исключения писал ровный `status='failed'` с
одним `error_msg`. Для владельца это «ничего не вышло» — хотя рассылка, упавшая
на 150-м адресате из 380, уже израсходовала 150 приглашений и дневные лимиты
аккаунтов. По модели состояний недоведённая работа — `partial`, и именно по
этому различию решают, продолжать ли и с какого места.

Вместе со статусом терялось всё остальное, что считается итогом:
`result` со счётчиками, метрика `infragram_operations_total` (самая важная
категория исхода не попадала на график вовсе), подписанная запись
`compliance_engine.record` (а аудит-трейл обещает единый choke point и ВСЕ
операции) и событие шины `op_done`. В аудит-трейле исход был жёстко прописан
как «failed» даже когда часть целей отработана.

Счётчики после исключения берутся из журнала целей: своих исполнитель вернуть
не успел.
"""
from __future__ import annotations

import asyncio
import os
import re

from services import op_worker, op_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _crash_branch() -> str:
    """Ветка `if not requeued:` пути исключения — от неё и до конца обработчика."""
    body = _read("services/op_worker.py")
    start = body.index("async def _run_op_task(")
    end = body.index("async def _exec_bulk_bot_edit")
    task = body[start:end]
    i = task.index("            if not requeued:")
    return task[i:]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Счётчики по журналу ──────────────────────────────────────────────────────

def test_journal_counters_return_what_the_journal_says(monkeypatch):
    captured: dict = {}

    async def _fetchrow(pool, query, *args, **kw):
        captured["query"] = query
        return {"ok_n": 150, "failed_n": 7}

    async def _family(pool, op_id):
        return [op_id, op_id + 1]

    monkeypatch.setattr(op_worker, "_safe_fetchrow", _fetchrow)
    monkeypatch.setattr(op_worker, "journal_op_ids", _family)

    assert _run(op_worker._journal_counters(None, 42)) == (150, 7)
    assert "operation_log" in captured["query"]


def test_journal_counters_count_targets_not_rows(monkeypatch):
    """У одной цели бывает 'error', а за ним 'ok' — это один успех, не два итога.

    Проверяем формулировку запроса: без живой БД семантику FILTER/bool_or
    подтвердить нечем, а именно она отличает «цель» от «строки журнала».
    """
    captured: dict = {}

    async def _fetchrow(pool, query, *args, **kw):
        captured["query"] = query
        return {"ok_n": 1, "failed_n": 0}

    async def _family(pool, op_id):
        return [op_id]

    monkeypatch.setattr(op_worker, "_safe_fetchrow", _fetchrow)
    monkeypatch.setattr(op_worker, "journal_op_ids", _family)
    _run(op_worker._journal_counters(None, 1))

    q = re.sub(r"\s+", " ", captured["query"])
    assert "GROUP BY target" in q, "иначе считаются строки журнала, а не цели"
    assert "bool_or(status='ok')" in q, (
        "цель с поздним успехом обязана считаться успехом, а не обоими итогами")
    assert "status IN ('ok', 'error')" in q, (
        "'info' — примечание исполнителя, а не приговор цели")


def test_journal_counters_are_fail_open(monkeypatch):
    """Нет журнала или сбой чтения — остаёмся при прежнем поведении, не падаем."""
    async def _boom(*a, **k):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(op_worker, "_safe_fetchrow", _boom)
    assert _run(op_worker._journal_counters(None, 1)) == (0, 0)

    async def _none(*a, **k):
        return None

    async def _family(pool, op_id):
        return [op_id]

    monkeypatch.setattr(op_worker, "_safe_fetchrow", _none)
    monkeypatch.setattr(op_worker, "journal_op_ids", _family)
    assert _run(op_worker._journal_counters(None, 1)) == (0, 0)


# ── Классификация исхода ─────────────────────────────────────────────────────

def test_crash_after_real_work_is_partial_not_failed():
    """Та же классификация, что на успешном пути: 150 из 380 — это не провал."""
    assert op_status.classify_final(op_status.FAILED, ok=150, failed=0) == op_status.PARTIAL
    assert op_status.classify_final(op_status.FAILED, ok=0, failed=0) == op_status.FAILED


def test_the_crash_branch_asks_the_classifier():
    branch = _crash_branch()
    assert "_journal_counters(pool, op_id)" in branch, (
        "после исключения счётчики берутся только из журнала целей")
    assert "op_status.classify_final(" in branch, (
        "исход обязан решать классификатор, а не литерал 'failed'")
    assert "status='failed'" not in branch.split("_circuit_breaker_record")[0], (
        "статус провала жёстко прописан — partial никогда не получится")


def test_the_crash_branch_saves_the_counters():
    branch = _crash_branch()
    assert "result=$4::jsonb" in branch, (
        "без result владелец видит провал без цифр и не знает, что уже ушло")
    assert "acct_wait_since=NULL" in branch, (
        "иначе повтор наследует накопленное ожидание флота")


def test_the_crash_branch_is_visible_outside_the_log():
    """Три канала объявления переехали в одну дверь — проверяем и её, и вызов."""
    branch = _crash_branch()
    assert "_announce_op_outcome(" in branch, (
        "упавшие операции не попадали ни на график исходов, ни в аудит, ни в "
        "память организма")
    src = _read("services/op_worker.py")
    door = src[src.index("async def _announce_op_outcome("):]
    door = door[:door.index("\nasync def ", 10)]
    assert "infragram_operations_total" in door, (
        "общая дверь не ведёт на график исходов")
    assert "compliance_engine" in door, (
        "аудит-трейл обещает ВСЕ операции единым choke point")
    assert '"op_done"' in door, "память организма не узнаёт о завершении"


def test_the_audit_outcome_follows_the_status():
    branch = _crash_branch()
    assert "_crash_outcome" in branch
    assert re.search(r'"partial" if _crash_status == op_status\.PARTIAL', branch), (
        "операция, взявшая часть целей, не пишется в аудит чистым провалом")
    # и ни одного жёсткого "failed" в вызовах _audit этой ветки
    for m in re.finditer(r"await _audit\(", branch):
        call = branch[m.start():m.start() + 400]
        assert '"failed"' not in call.split(")")[0] + ")", (
            "исход аудита снова прописан литералом")


# ── Чего ломать нельзя ───────────────────────────────────────────────────────

def test_the_terminal_guard_survived():
    """Отмену владельца путь исключения переписывать не должен — на этом стоит
    отдельный храповик (tests/test_op_cancel_and_shutdown), здесь дублируем
    проверку рядом с изменённым запросом."""
    branch = _crash_branch()
    i = branch.index("UPDATE operation_queue SET status=$3")
    assert "sql_terminal_list" in branch[i:i + 400]


def test_the_zero_rows_probe_still_recognises_a_cancellation():
    """Ноль обновлённых строк = операция уже терминальна (почти всегда отмена):
    про неё не докладываем как об ошибке. Проба обязана остаться ПОСЛЕ записи."""
    branch = _crash_branch()
    assert 'endswith(" 0")' in branch
    assert branch.index("UPDATE operation_queue SET status=$3") < branch.index('endswith(" 0")')
