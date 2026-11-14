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
    """Поддельный пул. `linked_bc` — ответ на вопрос «есть ли у расписания рассылка».

    Вопрос этот появился не от вкуса: `broadcast_id` пишется ОТДЕЛЬНЫМ запросом
    после создания рассылки, и в зазоре между ними пусто значит не «работы не
    было», а «рассылка создана, но мы об этом не записали». Поэтому
    восстановление спрашивает саму рассылку по `broadcasts.scheduled_id`, и
    подделка обязана отвечать на этот вопрос ОТДЕЛЬНО от вставки продления:
    один ответ 999 на любой fetchval закрывал бы расписание всегда, то есть
    тест «вернулось в очередь» проходил бы по неверной причине.
    """

    def __init__(self, rows, linked_bc=None):
        self.rows = rows
        self.linked_bc = linked_bc
        self.executed: list[tuple[str, tuple]] = []
        self.inserted: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        return self.rows

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        if "FROM broadcasts" in query:
            return self.linked_bc
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


@pytest.mark.asyncio
async def test_a_broadcast_without_its_link_is_not_fired_again():
    """Связь не записалась, а рассылка создана — расписание закрывается.

    Самый опасный зазор планировщика: `create_broadcast` прошёл, а
    `UPDATE scheduled_broadcasts SET broadcast_id` — нет (процесс умер на
    деплое, база не приняла запрос). Рассылка при этом уже стоит в 'pending', и
    на следующем старте процесса её поднимает `broadcaster.resume_interrupted`,
    то есть сообщение УЙДЁТ. Если восстановление поверит пустому
    `broadcast_id`, расписание вернётся в очередь и заведёт ВТОРУЮ рассылку —
    вся аудитория получит сообщение дважды.
    """
    from services import scheduler

    pool = _Pool([_row(broadcast_id=None)], linked_bc=321)
    stats = await scheduler.recover_stuck_claims(pool)

    assert stats == {"closed": 1, "requeued": 0}, (
        "расписание с уже созданной рассылкой вернулось в очередь — это второй "
        "залп на всю аудиторию")
    queries = [q for q, _ in pool.executed]
    assert any("status='done'" in q for q in queries)
    assert not any("status='pending'" in q for q in queries)
    assert any("broadcast_id=$2" in q for q in queries), (
        "связь не дописана: следующий сторож будет задавать тот же вопрос снова")


@pytest.mark.asyncio
async def test_an_unreadable_link_leaves_the_claim_alone():
    """Сбой чтения связи — не ответ «рассылки нет».

    fail-open здесь означал бы второй залп на всю аудиторию, поэтому занятие
    остаётся как есть: следующий проход сторожа спросит снова.
    """
    from services import scheduler

    class _BrokenLink(_Pool):
        async def fetchval(self, query, *args):
            if "FROM broadcasts" in query:
                raise RuntimeError("БД недоступна")
            return await super().fetchval(query, *args)

    pool = _BrokenLink([_row(broadcast_id=None)])
    stats = await scheduler.recover_stuck_claims(pool)

    assert stats == {"closed": 0, "requeued": 0}
    assert not pool.executed, (
        "на сбое чтения связи расписание всё равно тронули — решение принято "
        "без ответа на вопрос, была ли рассылка")


@pytest.mark.asyncio
async def test_the_link_is_written_by_the_same_insert_as_the_broadcast():
    """Обратная ссылка не имеет собственного зазора: её пишет тот же INSERT."""
    import inspect

    from database import db

    src = inspect.getsource(db.create_broadcast)
    assert "scheduled_id" in src, (
        "create_broadcast больше не принимает ссылку на расписание — "
        "восстановлению снова нечего спросить у самой рассылки")
    assert "scheduled_id)" in src and "INSERT INTO broadcasts" in src, (
        "ссылка на расписание пишется не тем же INSERT, что сама рассылка — "
        "значит у неё снова есть зазор, в котором рассылка есть, а связи нет")

    sched = inspect.getsource(__import__("services.scheduler", fromlist=["run"]).run)
    assert "scheduled_id=int(row[\"id\"])" in sched, (
        "планировщик не передаёт ссылку на расписание при создании рассылки")
