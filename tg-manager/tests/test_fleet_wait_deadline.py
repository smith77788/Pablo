"""Предел ожидания флота отсчитывается от ожидания, а не от постановки.

ЧТО БЫЛО. Операция, не нашедшая свободных аккаунтов, мягко откладывается на 90
секунд и ждёт, пока флот освободится. Ждать ей разрешено _ACCT_WAIT_MAX_MIN,
после чего она честно проваливается — иначе получился бы вечный цикл. Но
отсчёт вёлся от `created_at`, то есть от МОМЕНТА ПОСТАНОВКИ.

Для операции, запускаемой сразу, разница незаметна. Для всего остального она
фатальна: отложенный пост, поставленный утром на вечер; очередной круг
рекуррентного автопостинга; повтор после флуд-паузы (PeerFlood откладывает на
48 часов); операция, возвращённая предохранителем. Все они к моменту запуска
заведомо старше двадцати минут, поэтому ПЕРВАЯ же встреча с занятым флотом
проваливала их мгновенно, с текстом «не дождались свободных аккаунтов» — хотя
не ждали нисколько.

Владелец видел отложенную рассылку, упавшую ровно в назначенное время, без
единой попытки.
"""
from __future__ import annotations

import datetime as dt

import pytest


class _Pool:
    def __init__(self, waiting_since=None, has_column=True):
        self.row = {"acct_wait_since": waiting_since} if has_column else None
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        if self.row is None:
            raise RuntimeError('column "acct_wait_since" does not exist')
        return self.row

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def _minutes_ago(n: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=n)


@pytest.mark.asyncio
async def test_long_scheduled_operation_gets_its_full_wait():
    """Ключевой случай: операция поставлена вчера, ждать ещё не начинала."""
    from services import op_worker

    pool = _Pool(waiting_since=None)
    await op_worker._requeue_op_no_accounts(pool, 7)

    query = pool.executed[0][0]
    assert "status='pending'" in query, (
        "отложенная операция провалена на первой же встрече с занятым флотом, "
        "не прождав ни секунды"
    )
    assert "COALESCE(acct_wait_since, now())" in query, (
        "отметка начала ожидания не ставится или сдвигается каждым откладыванием "
        "— во втором случае предел не наступит никогда и вернётся вечный цикл"
    )


@pytest.mark.asyncio
async def test_operation_that_really_waited_too_long_fails():
    """Обратная сторона: предел обязан наступать, иначе это вечный цикл."""
    from services import op_worker

    pool = _Pool(waiting_since=_minutes_ago(op_worker._ACCT_WAIT_MAX_MIN + 5))
    await op_worker._requeue_op_no_accounts(pool, 7)

    query = pool.executed[0][0]
    assert "status='failed'" in query
    assert "status NOT IN" in query, "провал затрёт отмену владельца"


@pytest.mark.asyncio
async def test_operation_still_within_the_limit_keeps_waiting():
    from services import op_worker

    pool = _Pool(waiting_since=_minutes_ago(max(1, op_worker._ACCT_WAIT_MAX_MIN - 5)))
    await op_worker._requeue_op_no_accounts(pool, 7)

    assert "status='pending'" in pool.executed[0][0]


@pytest.mark.asyncio
async def test_naive_timestamp_does_not_crash_the_comparison():
    """Наивное время из драйвера не должно ронять путь возврата в очередь."""
    from services import op_worker

    naive = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).replace(tzinfo=None)
    pool = _Pool(waiting_since=naive)
    await op_worker._requeue_op_no_accounts(pool, 7)
    assert pool.executed, "сравнение времени упало — операция осталась в 'running'"


@pytest.mark.asyncio
async def test_missing_column_makes_the_operation_wait_not_fail():
    """Деплой обогнал миграцию: ждать дольше положенного безопаснее, чем терять
    работу владельца."""
    from services import op_worker

    pool = _Pool(has_column=False)
    await op_worker._requeue_op_no_accounts(pool, 7)
    assert "status='pending'" in pool.executed[0][0]


def test_mark_is_cleared_once_the_executor_actually_ran():
    """Иначе следующий повтор этой же операции унаследует чужое ожидание."""
    import inspect

    from services import op_worker

    done = inspect.getsource(op_worker._run_op_task)
    assert "acct_wait_since=NULL" in done, (
        "отметка не сбрасывается после отработки — повтор по другой причине "
        "провалится на первом же занятом флоте"
    )
    retry = inspect.getsource(op_worker._maybe_requeue)
    assert "acct_wait_since=NULL" in retry, (
        "повтор по ошибке исполнителя — новая попытка сделать работу, а не "
        "продолжение ожидания флота"
    )
