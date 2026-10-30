"""Защитная запись, съеденная ОТМЕНОЙ задачи, обязана оставить след.

РАЗРЫВ. Три писателя защиты (`_journal_write`, `_journal_writemany`,
`_retrying_execute`) построены одинаково: попытка, пауза, вторая попытка, потом
`log.error` и счётчик — «чтобы риск дубля был виден на графике, а не только в
логе». Ловят они `except Exception`. А `asyncio.CancelledError` с Python 3.8 не
`Exception`, поэтому отмена проходила сквозь всю эту конструкцию: ни лога, ни
счётчика, ни единого следа. Вся система защитных оповещений была слепа ровно к
тому пути, которым пользуются наши собственные остановки — `shutdown` по
SIGTERM и `_stand_down_after_lease_loss` при передаче очереди.

ЧЕМ ЭТО ПЛОХО ИМЕННО ЗДЕСЬ. `_journal_write` сама объясняет, что пишется
ПОСЛЕ доставки: «цель уже отработана, отменять нечего». Отмена, попавшая между
действием и отметкой, теряет отметку о СОСТОЯВШЕМСЯ действии. А `shutdown`
возвращает свои операции в очередь, то есть повторный прогон не «возможен», он
ГАРАНТИРОВАН следующим процессом — и этот прогон сделает цель второй раз:
второе сообщение человеку, второй пост, второе приглашение. Автодеплой означает
рестарт на каждый пуш, так что путь этот не экзотический.

ПОЧЕМУ СЧЁТЧИК ОТДЕЛЬНЫЙ И НЕ БУДИТ ВЛАДЕЛЬЦА. Потеря по отмене — не отказ
базы, и поднимать ею тревогу `_PROTECTIVE_FAILURE_COUNTERS` нельзя: деплой
штатный и частый, а оповещение, приходящее на каждый выпуск, владелец
перестанет читать — и пропустит настоящий сбой защиты. Поэтому отдельный
счётчик `infragram_protective_write_cancelled_total` (не `*_failures_total`,
чтобы храповик защитных счётчиков не требовал его в списке тревог) и WARNING в
логе: потеря перестаёт быть невидимой, но тревогу не обесценивает.

ОТМЕНА ПРОБРАСЫВАЕТСЯ ДАЛЬШЕ. Проглотить её нельзя ни в коем случае: задача,
не заметившая отмены, продолжает работать сессией, аренда которой уже снята, —
это AUTH_KEY_DUPLICATED. Контракт писателей при этом не меняется: ошибок они
по-прежнему не бросают, а отмена проходила сквозь них и раньше.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

CANCELLED = "infragram_protective_write_cancelled_total"


@pytest.fixture
def ow():
    from services import op_worker
    yield op_worker


@pytest.fixture(autouse=True)
def _fresh_metrics():
    from services import metrics as _m
    _m.reset()
    yield
    _m.reset()


def _counter(name: str) -> float:
    from services import metrics as _m
    counters = (_m.snapshot() or {}).get("counters") or {}
    return sum(float(v) for k, v in counters.items()
               if k == name or k.startswith(name + "{"))


class _CancellingPool:
    """Пул, у которого запись попадает под отмену задачи."""

    async def execute(self, q, *a):
        raise asyncio.CancelledError()

    async def executemany(self, q, rows):
        raise asyncio.CancelledError()


class _BrokenPool:
    """Пул, у которого запись отказывает по-настоящему (не отмена)."""

    async def execute(self, q, *a):
        raise RuntimeError("БД недоступна")

    async def executemany(self, q, rows):
        raise RuntimeError("БД недоступна")


def _call(ow, name, pool):
    if name == "_journal_writemany":
        return ow._journal_writemany(
            pool, "INSERT INTO operation_log VALUES ($1)", [(1,)],
            log_ctx="[t]", where="t")
    fn = getattr(ow, name)
    return fn(pool, "UPDATE operation_queue SET status='done'",
              log_ctx="[t]", where="t")


WRITERS = ["_journal_write", "_journal_writemany", "_retrying_execute"]


# ── Главное: отмена оставляет след и летит дальше ────────────────────────────

@pytest.mark.parametrize("writer", WRITERS)
def test_a_cancelled_write_is_counted_and_reraised(ow, writer, caplog):
    """Счётчик, WARNING и проброс отмены — все три обязательны."""
    async def _go():
        with caplog.at_level(logging.WARNING, logger="services.op_worker"):
            with pytest.raises(asyncio.CancelledError):
                await _call(ow, writer, _CancellingPool())

    asyncio.run(_go())

    assert _counter(CANCELLED) >= 1, (
        f"{writer}: запись съела отмена, а следа нет — потеря невидима"
    )
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.levelno >= logging.WARNING)
    assert text.strip(), f"{writer}: ни строчки в логе об отменённой записи"


@pytest.mark.parametrize("writer", WRITERS)
def test_a_real_database_failure_still_goes_the_old_way(ow, writer):
    """Самопроверка мерки: отказ БД — это НЕ отмена, и счётчик отмен молчит.

    Счётчик, срабатывающий на любой потере, не отличал бы деплой от сбоя базы,
    то есть не измерял бы ничего.
    """
    async def _go():
        return await _call(ow, writer, _BrokenPool())

    res = asyncio.run(_go())

    assert res == "ERROR", f"{writer}: отказ БД перестал возвращать ERROR: {res!r}"
    assert _counter(CANCELLED) == 0, (
        f"{writer}: счётчик отмен сработал на обычном отказе БД — мерка сломана"
    )


def test_the_cancelled_counter_is_not_an_owner_alarm(ow):
    """Он НЕ в списке тревог: деплой штатный, а тревога на каждый выпуск — шум."""
    assert CANCELLED not in ow._PROTECTIVE_FAILURE_COUNTERS, (
        "счётчик отмен попал в список тревог владельца — он будет приходить на "
        "каждый деплой, и настоящие сбои защиты потеряются в этом шуме"
    )


def test_the_counter_is_documented(ow):
    """Описание графика обязано быть: безымянный счётчик никто не прочтёт."""
    from services import metrics as _m
    assert CANCELLED in _m._HELP, "счётчик не описан в services/metrics.py::_HELP"
