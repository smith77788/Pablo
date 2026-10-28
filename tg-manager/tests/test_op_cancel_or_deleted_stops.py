"""Отменённая ИЛИ удалённая операция обязана останавливать исполняющуюся задачу.

Жалоба владельца: «исполняются операции, которые давно удалены или отменены».
Корень: op_worker._is_cancelled при ОТСУТСТВИИ строки (операция удалена из
operation_queue) возвращал False — то есть «не отменена», и задача доигрывала до
конца. Теперь нет строки = удалена = стоп; при транзиентной ошибке БД живую
операцию не рвём (вернём прошлый кэш/False).
"""
from __future__ import annotations

import asyncio

import services.op_worker as ow


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Pool:
    def __init__(self, row=None, raise_exc=None):
        self._row = row
        self._raise = raise_exc

    async def fetchrow(self, q, *a):
        if self._raise:
            raise self._raise
        return self._row


def _clear():
    ow._cancel_cache.clear()


def test_deleted_operation_is_treated_as_cancelled():
    _clear()
    # Строки нет (операция удалена) → задача обязана остановиться.
    assert _run(ow._is_cancelled(_Pool(row=None), 123)) is True


def test_cancelled_status_stops():
    _clear()
    assert _run(ow._is_cancelled(_Pool(row={"status": "cancelled"}), 1)) is True


def test_running_operation_not_cancelled():
    _clear()
    assert _run(ow._is_cancelled(_Pool(row={"status": "running"}), 2)) is False


def test_transient_db_error_does_not_abort_live_op():
    _clear()
    # Нет кэша + ошибка БД → НЕ трактуем как отмену (не рвём живую операцию).
    assert _run(ow._is_cancelled(_Pool(raise_exc=RuntimeError("db blip")), 3)) is False


def test_db_error_keeps_prior_cached_verdict():
    _clear()
    # Прогрели кэш «отменена», затем БД моргнула — держим прошлый вердикт.
    ow._cancel_cache[7] = (True, -10_000.0)  # старый, чтобы обойти TTL
    assert _run(ow._is_cancelled(_Pool(raise_exc=RuntimeError("blip")), 7)) is True
