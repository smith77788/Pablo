"""Ошибка отчёта превращала выполненную операцию в провал.

РАЗРЫВ. Обработчик ошибок в `_run_op_task` накрывает весь успешный путь —
включая всё, что идёт ПОСЛЕ записи терминального статуса: сообщение владельцу,
графики, подписанный аудит, память организма. Любое исключение там попадало в
разбор провала:

* владелец получал «❌ операция провалилась» вслед за «✅»;
* аккаунтам операции писался неуспех (`record_account_op(success=False)`) — а
  это trust аккаунта и давление флота, то есть губернатор замедлял темп ВСЕХ
  операций владельца из-за неудачной отправки сообщения;
* на графике одна операция давала два разных исхода.

Сам статус в БД при этом не менялся: и запись провала, и `_maybe_requeue`
стоят под `status NOT IN terminal`. То есть от «провала» оставались ровно
последствия и ни одного полезного решения.

Откуда там исключение — проверено по коду: графики, аудит и уведомление
бросить не могут (у `_announce_op_outcome` каждый канал в своём try, `_audit`
обещает «никогда не бросает», `db.notify_if_enabled` повторяет отправку и
глотает отказ Bot API). Не защищена сборка клавиатуры к сообщению об итоге:
`CallbackData.pack()` в aiogram бросает ValueError на данных кнопки длиннее 64
байт или с разделителем внутри. Там же, после закрытия, идёт продление
расписания `_reschedule_recurring`.

ЧТО ПРОВЕРЯЕМ. Перед разбором провала стоит вопрос «а операция точно ещё не
закрыта?»: закрыта — пишем log.error со счётчиком и не трогаем ни исход, ни
память организма. Не прочитали статус — идём обычным путём: доложить о провале,
которого не было, хуже, чем промолчать о настоящем.
"""
from __future__ import annotations

import ast
import os

import pytest

from services import op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")


class _Pool:
    def __init__(self, status: str | None = "done", *, broken: bool = False):
        self.status = status
        self.broken = broken
        self.reads = 0

    async def fetchrow(self, query, *args):
        self.reads += 1
        if self.broken:
            raise RuntimeError("БД недоступна")
        return None if self.status is None else {"status": self.status}


@pytest.fixture
def metric(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(op_worker, "_reliability_metric",
                        lambda name, *a, **kw: seen.append((name, kw)))
    monkeypatch.setattr(op_worker, "_JOURNAL_READ_RETRY_DELAY_S", 0.0)
    return seen


@pytest.mark.parametrize("status", ["done", "partial", "failed", "cancelled"])
@pytest.mark.asyncio
async def test_a_closed_operation_is_not_failed_again(metric, status):
    assert await op_worker._failed_after_the_op_was_closed(
        _Pool(status), 42, "bulk_join", RuntimeError("429 от Bot API")) is True, (
        f"исключение после закрытия со статусом {status} снова разбирается как "
        "провал: владельцу уйдёт «❌», а аккаунтам — неуспех")
    assert [n for n, _ in metric] == ["infragram_op_report_after_close_total"]


@pytest.mark.asyncio
async def test_a_live_operation_still_fails_normally(metric):
    """Самопроверка: настоящий провал исполнителя разбирается как раньше."""
    for status in ("running", "pending"):
        assert await op_worker._failed_after_the_op_was_closed(
            _Pool(status), 42, "bulk_join", RuntimeError("таймаут")) is False
    assert not metric, "обычный провал не должен попадать в этот счётчик"


@pytest.mark.asyncio
async def test_an_unreadable_status_takes_the_normal_path(metric):
    """Промолчать о настоящем провале хуже, чем доложить о лишнем."""
    pool = _Pool(broken=True)
    assert await op_worker._failed_after_the_op_was_closed(
        pool, 42, "bulk_join", RuntimeError("таймаут")) is False
    assert pool.reads == 2, "решение принято без повтора чтения"


@pytest.mark.asyncio
async def test_a_missing_row_takes_the_normal_path(metric):
    assert await op_worker._failed_after_the_op_was_closed(
        _Pool(None), 42, "bulk_join", RuntimeError("таймаут")) is False


# ── Место охранника в обработчике ────────────────────────────────────────────

def _failure_handler() -> ast.ExceptHandler:
    """Обработчик, который накрывает успешный путь `_run_op_task`."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_run_op_task")
    # Нужен самый ДЛИННЫЙ except Exception внутри функции: именно он накрывает
    # весь успешный путь. Границы берём по AST, а не окном в символах.
    handlers = [h for t in ast.walk(fn) if isinstance(t, ast.Try)
                for h in t.handlers
                if h.type is not None and ast.unparse(h.type) == "Exception"]
    assert handlers, "обработчик провала пропал из _run_op_task"
    return max(handlers, key=lambda h: h.end_lineno - h.lineno)


def test_the_guard_stands_before_every_consequence_of_a_failure():
    handler = _failure_handler()
    src = ast.unparse(handler)
    at = src.index("_failed_after_the_op_was_closed")
    for marker, why in (
        ("record_account_op", "аккаунтам запишется неуспех: trust и темп флота"),
        ("_maybe_requeue", "операция пойдёт на повтор уже после закрытия"),
        ("_announce_op_outcome", "владельцу уйдёт «❌» вслед за «✅»"),
    ):
        assert marker in src, f"{marker} исчез из обработчика — проверка смотрит не туда"
        assert at < src.index(marker), (
            f"охранник стоит ПОСЛЕ {marker}: {why}")
