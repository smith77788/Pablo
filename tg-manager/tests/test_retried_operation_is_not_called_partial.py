"""Доведённая со второй попытки операция — «выполнено», а не «частично».

ЧТО БЫЛО. Счётчик прогресса `done_items` обнуляется на КАЖДОЙ новой попытке:
так делает `_maybe_retry_op` и все пути возврата операции в очередь. А
`total_items` остаётся от первого замаха. Значит у операции на 380 целей,
которая взяла 203, ушла в повтор и добрала остальные 177, на момент закрытия в
очереди лежит `done_items=177` при `total_items=380`.

`classify_final` ловит недобор по прогрессу — ровно та проверка, которая не даёт
закрыть зелёным 203 из 380. Здесь она срабатывала на ПОЛНОСТЬЮ выполненной
работе: операция, закрывшая все 380 целей, уходила владельцу как «частично».
После циклов про честные исходы цена выросла: такую операцию начали предлагать
к повтору и присылать по ней эскалацию — то есть продукт сам себя дёргал
по завершённой работе.

Журнал целей обнуления не знает: он пишется по цели и живёт между попытками.
ФИКС: прогресс на закрытии берём как максимум из счётчика очереди и покрытия
журнала (`_own_journal_coverage`), и этим же значением поправляем `done_items`,
чтобы полоса прогресса не спорила со статусом.

ПОЧЕМУ НЕ `_journal_counters`. Тот читает всю цепочку повторов, включая
родителя. У дочерней операции-повтора свой `total_items` — только по упавшим
целям родителя, и цели родителя раздували бы её покрытие до «перевыполнено».
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from services import op_status, op_worker

_SRC = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


class _LogPool:
    """Журнал целей в памяти; запрос применяется по смыслу, а не как заглушка."""

    def __init__(self, rows, *, raises=False):
        self.rows = rows
        self.raises = raises
        self.seen: list[str] = []

    async def fetchrow(self, query, *args):
        self.seen.append(query)
        if self.raises:
            raise RuntimeError("журнал недоступен")
        assert "count(DISTINCT target)" in query, (
            "покрытие считается не по РАЗНЫМ целям: одна цель с 'error' и "
            f"следующим 'ok' дала бы двойной зачёт — {query}")
        assert "op_id=$1" in query, (
            "покрытие берётся не у этой операции: цели родителя раздули бы "
            f"покрытие дочернего повтора — {query}")
        op_id = args[0]
        targets = {r["target"] for r in self.rows
                   if r["op_id"] == op_id and r["status"] in ("ok", "error")}
        return {"n": len(targets)}

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


def _log(op_id, target, status="ok"):
    return {"op_id": op_id, "target": target, "status": status}


def test_coverage_counts_distinct_targets_across_attempts():
    """Две попытки одной операции складываются в одно покрытие."""
    rows = [_log(5001, f"u{i}") for i in range(203)]          # первая попытка
    rows += [_log(5001, f"u{i}") for i in range(203, 380)]    # вторая
    # та же цель, закрытая ошибкой и потом успехом, считается один раз
    rows += [_log(5001, "u7", "error")]
    pool = _LogPool(rows)

    n = asyncio.run(op_worker._own_journal_coverage(pool, 5001))
    assert n == 380, f"покрытие журнала посчитано неверно: {n}"


def test_coverage_ignores_other_operations():
    pool = _LogPool([_log(5001, "a"), _log(9999, "b"), _log(9999, "c")])
    assert asyncio.run(op_worker._own_journal_coverage(pool, 5001)) == 1


def test_coverage_is_zero_when_journal_unreadable():
    """Журнала нет или чтение упало — поведение прежнее, не исключение."""
    pool = _LogPool([], raises=True)
    assert asyncio.run(op_worker._own_journal_coverage(pool, 5001)) == 0


def test_the_class_itself_without_the_journal():
    """Что именно решалось неверно: прогресс одной попытки против всей цели."""
    по_очереди = op_status.classify_final(
        "done", ok=177, failed=0, done_items=177, total_items=380)
    по_журналу = op_status.classify_final(
        "done", ok=177, failed=0, done_items=380, total_items=380)
    assert по_очереди == op_status.PARTIAL
    assert по_журналу == op_status.DONE, (
        "операция закрыла все 380 целей, а исход всё равно не «выполнено»")


def _close_block() -> str:
    """Текст функции, которая закрывает успешный прогон операции."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_op_task":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("в op_worker.py нет функции _run_op_task")


def test_close_path_asks_the_journal_about_progress():
    """Храповик: закрытие не возвращается к счётчику одной попытки."""
    body = _close_block()
    assert "_own_journal_coverage(pool, op_id)" in body, (
        "путь закрытия снова судит о прогрессе только по done_items из очереди: "
        "доведённая со второй попытки операция опять станет «частичной»")
    assert "done_items=_done_n" in body.replace(" ", ""), (
        "в classify_final уходит не исправленный прогресс")


def test_journal_only_confirms_full_coverage_and_never_inflates():
    """Журнал отвечает на один вопрос: закрыта ли ВСЯ цель.

    Живой Postgres показал, за что здесь нельзя брать «максимум из двух»:
    журнал пишут не только цели. Инвайт кладёт туда служебную строку про
    фолбэк-ссылку, и прогресс выходил на единицу больше реальной работы —
    done_items=21 при цели 20 и 13 при 12. Счётчик прогресса врать не должен ни
    в одну сторону, поэтому покрытие лишь подтверждает полноту.
    """
    flat = " ".join(_close_block().split())
    assert "_done_n = _goal if (_goal > 0 and _covered >= _goal) else _done_q" in flat, (
        "прогресс снова считается максимумом из счётчика и покрытия журнала — "
        "служебные строки журнала будут завышать его")


def test_close_path_repairs_the_progress_counter():
    """Статус и полоса прогресса не должны спорить друг с другом."""
    body = _close_block().replace(" ", "")
    assert "done_items=GREATEST(COALESCE(done_items,0),$5::int)" in body, (
        "done_items не поправлен на закрытии: статус скажет «выполнено», "
        "а полоса рядом — «177 из 380»")
