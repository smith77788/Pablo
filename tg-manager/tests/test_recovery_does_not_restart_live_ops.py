"""Движок самовосстановления не перезапускает РАБОТАЮЩУЮ операцию.

ЧТО БЫЛО. В продукте два независимых сторожа зависших операций:

  * `op_worker._watchdog_stale` — порог 60 минут, но он ЯВНО исключает операции,
    которые прямо сейчас выполняются в памяти процесса (`_active_op_ids`);
  * `recovery_engine._queue_recovery` — порог 90 минут и НИКАКИХ исключений.

Оба крутятся в одном и том же процессе роли worker. Значит второй сторож видел
ровно то, что первый сознательно щадил: живую массовую операцию. А массовой
операции идти часами — норма, это пейсинг против банов, и собственный потолок
прогона у неё 6 часов (`asyncio.wait_for(timeout_for(...))`). На 90-й минуте
здоровый mass_invite возвращался в `pending`, поллер тут же забирал его снова —
и та же операция шла ВТОРЫМ прогоном по тем же аккаунтам, параллельно первому.
Это ровно тот класс двойного исполнения, от которого бережётся всё остальное
(аренда аккаунтов, дедуп в шине, отсечка `_active_op_ids` у вотчдога и у
алертов), и он опасен банами. Заодно у исправной операции сгорала единица
`retry_count`, приближая её к dead letter.

ФИКС двухслойный:
  1. та же отсечка `_active_op_ids`, что у вотчдога и у алертов;
  2. порог считается ОТ потолка прогона операции, а не задан числом: пока
     потолок не вышел, операция не зависла — её снимет собственный таймаут.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import op_worker, recovery_engine


class _FakePool:
    """Пул, который ЧЕСТНО применяет предикаты запроса к строкам в памяти.

    Важно именно так: фильтр живёт в SQL, и заглушка, возвращающая строки
    как есть, не отличила бы фикс от его отсутствия.
    """

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        if "FROM operation_queue" not in query:
            return []
        rows = [r for r in self.rows if r["status"] == "running"]

        m = re.search(r"\$(\d+)::bigint\[\] IS NULL OR id != ALL", query)
        if m:
            excluded = args[int(m.group(1)) - 1]
            if excluded:
                skip = {int(i) for i in excluded}
                rows = [r for r in rows if int(r["id"]) not in skip]

        m = re.search(r"\$(\d+) \* INTERVAL '1 minute'", query)
        if m:
            threshold = float(args[int(m.group(1)) - 1])
            rows = [r for r in rows if float(r["stuck_minutes"]) > threshold]
        return rows

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return {"id": 1}

    def queue_writes(self) -> list[str]:
        return [q for q, _ in self.executed if "operation_queue" in q]


def _row(op_id, stuck_minutes, retry_count=0, op_type="mass_invite"):
    return {
        "id": op_id,
        "op_type": op_type,
        "account_id": None,
        "retry_count": retry_count,
        "max_retries": 3,
        "status": "running",
        "stuck_minutes": stuck_minutes,
    }


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _recover(pool, active_ids):
    op_worker._active_op_ids.clear()
    for i in active_ids:
        op_worker._active_op_ids.add(int(i))
    try:
        return _run(recovery_engine._queue_recovery(pool, None, 777))
    finally:
        op_worker._active_op_ids.clear()


def test_threshold_is_above_operation_own_timeout():
    """Порог сторожа обязан быть ПОЗЖЕ потолка прогона операции.

    Иначе сторож конкурирует с таймаутом исполнителя и бьёт по живой работе:
    до потолка операция не зависла, а идёт.
    """
    cap_min = op_worker._OP_TIMEOUT_DEFAULT_S / 60
    assert recovery_engine._queue_stuck_minutes() > cap_min, (
        "сторож срабатывает раньше, чем истекает собственный потолок операции — "
        "он сбрасывает работающие операции, а не зависшие"
    )


def test_live_operation_is_not_requeued():
    # Возраст заведомо больше порога, но операция РЕАЛЬНО исполняется в процессе.
    old = recovery_engine._queue_stuck_minutes() + 120
    pool = _FakePool([_row(14, old)])
    actions = _recover(pool, active_ids={14})
    assert pool.queue_writes() == [], (
        "работающая операция возвращена в очередь — она пойдёт вторым прогоном "
        "по тем же аккаунтам"
    )
    assert actions == []


def test_orphan_operation_is_still_requeued():
    # Та же строка, но в памяти процесса её нет: сирота после падения воркера.
    old = recovery_engine._queue_stuck_minutes() + 120
    pool = _FakePool([_row(14, old)])
    actions = _recover(pool, active_ids=set())
    writes = pool.queue_writes()
    assert writes, "брошенную running-операцию сторож обязан поднять"
    assert "status='pending'" in writes[0]
    assert len(actions) == 1 and actions[0].action == "resume"


def test_long_but_within_timeout_is_untouched_even_without_registry():
    """Долгая операция ниже потолка не трогается, даже если реестр пуст.

    Реестр `_active_op_ids` виден только своему процессу: при разделении ролей
    (web отдельно от worker) он пуст, и единственной защитой остаётся порог.
    Два часа для mass_invite — нормальная работа, а не зависание.
    """
    pool = _FakePool([_row(14, 120)])
    actions = _recover(pool, active_ids=set())
    assert pool.queue_writes() == []
    assert actions == []


def test_exhausted_orphan_goes_to_dead_letter():
    old = recovery_engine._queue_stuck_minutes() + 120
    pool = _FakePool([_row(14, old, retry_count=3)])
    actions = _recover(pool, active_ids=set())
    writes = pool.queue_writes()
    assert writes and "status='failed'" in writes[0]
    assert len(actions) == 1 and actions[0].action == "fail"


def test_unreadable_registry_touches_nothing(monkeypatch):
    """Не смогли прочитать реестр активных — ничего не сбрасываем.

    Цена ложного сброса — второй прогон по живым аккаунтам; цена паузы — минуты
    до следующего цикла восстановления.
    """
    async def _unknown():
        return None

    monkeypatch.setattr(recovery_engine, "_active_op_ids_here", _unknown)
    old = recovery_engine._queue_stuck_minutes() + 120
    pool = _FakePool([_row(14, old)])
    actions = _run(recovery_engine._queue_recovery(pool, None, 777))
    assert pool.queue_writes() == []
    assert actions == []
