"""Отложенная рассылка, брошенная рестартом, не пропадает навсегда.

ЧТО БЫЛО. Планировщик занимает строку расписания переводом в
`status='processing'` и снимает занятость только сам: помечает 'done' после
успеха либо возвращает 'pending' в своём `except`. Если процесс умирает МЕЖДУ
этими точками — а на Railway это каждый деплой — строка остаётся в 'processing'
НАВСЕГДА: `get_pending_scheduled` выбирает только 'pending', и ни один сторож
такие строки не смотрит. В отличие от очереди операций, где `_reset_stale_running`
и `_watchdog_stale` для этого и существуют, у расписаний восстановления не было
вовсе.

Снаружи: отложенная рассылка молча не уходит и молча не отчитывается, а у
повторяемого расписания вместе с ней обрывается вся дальнейшая цепочка — канал
просто перестаёт наполняться.

ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ. Вернуть такую строку в 'pending' вслепую: если процесс умер
ПОСЛЕ create_broadcast, второй запуск создал бы ВТОРУЮ рассылку на всю
аудиторию. Исход хуже исходной болезни, поэтому решение принимается по
broadcast_id, а не по одному лишь возрасту занятия.
"""
from __future__ import annotations

import pytest


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple[str, tuple]] = []
        self.inserted: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        return self.rows

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        self.inserted.append((query, args))
        return 999


def _row(**kw):
    base = {
        "id": 5, "bot_id": 100, "message_text": "привет", "created_by": 1,
        "execute_at": None, "repeat_interval_min": 0, "broadcast_id": None,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_abandoned_claim_without_broadcast_returns_to_the_queue():
    from services import scheduler

    pool = _Pool([_row(broadcast_id=None)])
    stats = await scheduler.recover_stuck_claims(pool)

    assert stats == {"closed": 0, "requeued": 1}
    query = pool.executed[0][0]
    assert "status='pending'" in query, "брошенное расписание не вернулось в очередь"
    assert "status='processing'" in query, (
        "нет условия на текущий статус — сторож перепишет строку, которую в эту "
        "секунду занял живой процесс"
    )


@pytest.mark.asyncio
async def test_abandoned_claim_with_broadcast_is_closed_not_refired():
    """Рассылка уже создана — второй запуск ушёл бы на всю аудиторию повторно."""
    from services import scheduler

    pool = _Pool([_row(broadcast_id=777)])
    stats = await scheduler.recover_stuck_claims(pool)

    assert stats == {"closed": 1, "requeued": 0}
    query = pool.executed[0][0]
    assert "status='done'" in query
    assert "status='pending'" not in query, (
        "расписание с уже созданной рассылкой возвращено в очередь — это дубль "
        "рассылки на всю аудиторию"
    )


@pytest.mark.asyncio
async def test_recurring_chain_survives_the_restart():
    """Продление повторяемого расписания не должно теряться при восстановлении."""
    from services import scheduler

    pool = _Pool([_row(broadcast_id=777, repeat_interval_min=60)])
    await scheduler.recover_stuck_claims(pool)

    assert pool.inserted, (
        "следующее вхождение повторяемого расписания не поставлено — цепочка "
        "оборвалась ровно там, где мы её чиним"
    )
    assert "INSERT INTO scheduled_broadcasts" in pool.inserted[0][0]


@pytest.mark.asyncio
async def test_unreadable_table_does_not_break_the_scheduler():
    from services import scheduler

    class _Broken(_Pool):
        async def fetch(self, query, *args):
            raise RuntimeError("БД недоступна")

    assert await scheduler.recover_stuck_claims(_Broken([])) == {"closed": 0, "requeued": 0}


def test_claim_records_when_it_happened():
    """Без отметки времени сторож не отличит живое занятие от брошенного."""
    import inspect

    from services import scheduler

    src = inspect.getsource(scheduler.run)
    assert "claimed_at=NOW()" in src, "занятие не отмечается временем"
    assert "broadcast_id=$2" in src, (
        "созданная рассылка не привязывается к расписанию — сторож не сможет "
        "отличить «работа не начиналась» от «рассылка уже создана»"
    )
    assert src.index("broadcast_id=$2") < src.index("broadcaster.start("), (
        "broadcast_id пишется после запуска задачи: смерть процесса в этом окне "
        "снова приведёт к дублю рассылки"
    )


def test_recovery_runs_at_startup():
    import inspect

    from services import scheduler

    src = inspect.getsource(scheduler.run)
    assert "await recover_stuck_claims(pool)" in src
    assert src.index("await recover_stuck_claims(pool)") < src.index("while True:"), (
        "сторож не отрабатывает на старте — а именно старт и следует за "
        "рестартом, оборвавшим занятия"
    )
