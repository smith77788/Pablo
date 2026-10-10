"""Цель Global Presence, брошенная на рестарте, не застывает навсегда.

ЧТО БЫЛО. Цель плана берётся атомарным переводом pending → running, и снимает
этот статус только тот же прогон. Воркер умер между занятием и исходом — деплой,
падение, потолок прогона — и цель остаётся в 'running' НАВСЕГДА: исполнитель при
повторе выбирает `status='pending'`, и брошенная цель больше не рассматривается
никогда. План молча не достраивается: владелец видит «22 из 30» и никакой
ошибки, потому что ошибки и не было. У операции есть сторожа зависших, у целей
внутри операции не было ничего.

ЧЕГО ДЕЛАТЬ НЕЛЬЗЯ. Вернуть такую цель в очередь вслепую. Между созданием
канала и записью результата стоит пауза 90-180 секунд (анти-детект при
установке имени) — окно в минуты, и смерть процесса в нём означает, что канал в
Telegram УЖЕ ЕСТЬ. Повтор создал бы второй такой же: лишнее дорогое действие
аккаунта и лишний повод для ограничений. Поэтому решает отметка созданного
ресурса, а не возраст цели.
"""
from __future__ import annotations

import inspect

import pytest


class _Pool:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_unstarted_targets_go_back_to_the_queue_and_created_ones_do_not():
    from services import op_worker

    pool = _Pool()
    await op_worker._revive_abandoned_gp_targets(pool, 42, "channel")

    assert len(pool.executed) == 2, "расклинивание не разделило два случая"
    requeue_q, close_q = pool.executed[0][0], pool.executed[1][0]

    assert "status='pending'" in requeue_q
    assert "result_asset_id IS NULL" in requeue_q, (
        "в очередь возвращаются цели, ресурс для которых уже создан — это "
        "второй канал или второй бот на те же деньги и тот же риск"
    )
    assert "status='done'" in close_q
    assert "result_asset_id IS NOT NULL" in close_q
    for q in (requeue_q, close_q):
        assert "status='running'" in q, (
            "нет условия на занятый статус — расклинивание перепишет цель, "
            "которую прямо сейчас выполняет живой прогон"
        )


@pytest.mark.asyncio
async def test_broken_database_does_not_break_the_operation():
    from services import op_worker

    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError("БД недоступна")

    await op_worker._revive_abandoned_gp_targets(_Broken(), 42, "channel")


def test_created_resource_is_recorded_before_the_slow_finishing_steps():
    """Отметка после паузы бесполезна: смерть процесса случится именно в паузе."""
    from services import op_worker

    src = inspect.getsource(op_worker._exec_global_presence_channel)
    mark = src.index("SET result_asset_id=$1")
    pause = src.index("waiting %.0fs before assigning username")
    assert mark < pause, (
        "созданный канал отмечается уже после паузы 90-180с — обрыв в этом "
        "окне снова оставит цель без следа, и повтор создаст второй канал"
    )


def test_created_bot_is_recorded_before_the_catalog_write():
    from services import op_worker

    src = inspect.getsource(op_worker._exec_global_presence_bot)
    assert "SET result_asset_id=$1" in src, "созданный бот не отмечается вовсе"
    assert src.index("SET result_asset_id=$1") < src.index("_db.add_bot("), (
        "бот отмечается после записи в каталог — обрыв между ними снова "
        "приведёт к созданию ВТОРОГО бота через BotFather"
    )


def test_both_executors_unstick_before_selecting_targets():
    from services import op_worker

    for fn in (op_worker._exec_global_presence_channel, op_worker._exec_global_presence_bot):
        src = inspect.getsource(fn)
        assert "_revive_abandoned_gp_targets" in src, f"{fn.__name__} не расклинивает цели"
        assert src.index("_revive_abandoned_gp_targets") < src.index(
            "WHERE plan_id=$1 AND status='pending'"
        ), f"{fn.__name__}: расклинивание идёт после выборки — брошенные цели в неё не попадут"
