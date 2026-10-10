"""Второе нажатие «Повторить» не переделывает работу первого повтора.

ЧТО ЛОМАЛОСЬ. Повтор — это новая строка очереди со ссылкой `retry_of_op` на
исходную операцию; по ней исполнитель читает журнал предка и не делает второй
раз то, что уже сделано. Обход шёл только ВВЕРХ, к предкам.

Беда в том, что исходная операция остаётся недоведённой НАВСЕГДА: её статус не
меняется оттого, что повтор доделал работу, и кнопка повтора на её экране
никуда не девается. Второе нажатие ставит ВТОРОЙ повтор — брата первого, а не
его потомка. Он видел журнал предка и ничего не знал о работе первого повтора:
каналы, опубликованные первым, получали второй пост, адресаты — второе
одинаковое сообщение. Хуже того, список «упавших целей» для второго повтора
собирался по журналу исходной операции, то есть в него намеренно попадали все
цели, которые первый повтор уже закрыл.

ЧТО ТЕПЕРЬ. Обход идёт в обе стороны: от операции вверх к корню и от всей
семьи вниз по повторам. Журнал и список упавших целей собираются по всей семье,
поэтому братья видят работу друг друга.
"""
from __future__ import annotations

import pytest


class _FamilyPool:
    """Очередь, где у операций есть ссылки `retry_of_op`, и журнал по op_id."""

    def __init__(self, parents: dict[int, int | None], journal: dict[int, list[tuple]] | None = None):
        self.parents = parents
        self.journal = journal or {}
        self.journal_ids: list = []

    async def fetchrow(self, query, *args):
        if "retry_of_op" in query and "SELECT params" in query:
            src = self.parents.get(int(args[0]))
            return {"src": str(src) if src is not None else None}
        return None

    async def fetch(self, query, *args):
        if "FROM operation_queue" in query and "retry_of_op" in query:
            wanted = {str(x) for x in args[0]}
            return [{"id": op} for op, parent in sorted(self.parents.items())
                    if parent is not None and str(parent) in wanted]
        if "operation_log" in query:
            ids = [int(x) for x in args[0]]
            self.journal_ids.append(sorted(ids))
            rows = []
            for op in ids:
                for target, status in self.journal.get(op, []):
                    rows.append({"target": target, "status": status})
            return rows
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_a_retry_sees_the_work_of_its_sibling():
    from services import operation_bus

    # 10 — исходная, 11 и 12 — два повтора от неё же.
    family = await operation_bus.retry_family_ids(_FamilyPool({10: None, 11: 10, 12: 10}), 12)

    assert sorted(family) == [10, 11, 12], (
        "второй повтор не видит работу первого — каналы, опубликованные первым, "
        "получат второй пост"
    )


@pytest.mark.asyncio
async def test_the_original_sees_every_repeat_made_from_it():
    from services import operation_bus

    family = await operation_bus.retry_family_ids(_FamilyPool({10: None, 11: 10, 12: 10}), 10)

    assert sorted(family) == [10, 11, 12]


@pytest.mark.asyncio
async def test_a_deep_chain_is_collected_from_any_point():
    """Повтор повтора: встав в середину, видим и предков, и потомков."""
    from services import operation_bus

    family = await operation_bus.retry_family_ids(
        _FamilyPool({10: None, 11: 10, 12: 11, 13: 12}), 11)

    assert sorted(family) == [10, 11, 12, 13]


@pytest.mark.asyncio
async def test_the_operation_itself_comes_first():
    from services import operation_bus

    family = await operation_bus.retry_family_ids(_FamilyPool({10: None, 11: 10}), 11)

    assert family[0] == 11


@pytest.mark.asyncio
async def test_broken_links_cannot_loop_forever():
    from services import operation_bus

    family = await operation_bus.retry_family_ids(_FamilyPool({21: 22, 22: 21}), 21)

    assert sorted(family) == [21, 22]
    assert len(family) <= operation_bus.RETRY_FAMILY_MAX


@pytest.mark.asyncio
async def test_an_unreadable_queue_falls_back_to_the_own_journal():
    from services import operation_bus

    class _Broken:
        async def fetchrow(self, query, *args):
            raise RuntimeError("база недоступна")

        async def fetch(self, query, *args):
            raise RuntimeError("база недоступна")

    assert await operation_bus.retry_family_ids(_Broken(), 7) == [7]


@pytest.mark.asyncio
async def test_targets_closed_by_a_sibling_are_not_offered_again():
    """Главный сценарий: А закрыт первым повтором, второй повтор берёт только Б."""
    from services import operation_bus

    pool = _FamilyPool(
        {10: None, 11: 10},
        journal={
            10: [("-100111", "error"), ("-100222", "error")],
            11: [("-100111", "ok"), ("-100222", "error")],
        },
    )

    failed = await operation_bus.collect_failed_targets(pool, 10, "mass_publish")

    assert failed == [-100222], (
        "второй повтор исходной операции забирает цели, которые первый повтор "
        "уже закрыл — они получат работу второй раз"
    )


@pytest.mark.asyncio
async def test_the_journal_query_covers_the_whole_family():
    from services import operation_bus

    pool = _FamilyPool({10: None, 11: 10, 12: 10}, journal={10: [("-100111", "error")]})
    await operation_bus.collect_failed_targets(pool, 10, "mass_publish")

    assert pool.journal_ids and pool.journal_ids[0] == [10, 11, 12]


@pytest.mark.asyncio
async def test_worker_idempotency_keys_use_the_same_family():
    from services import op_worker

    pool = _FamilyPool({10: None, 11: 10, 12: 10})
    assert sorted(await op_worker.journal_op_ids(pool, 12)) == [10, 11, 12], (
        "исполнитель повтора читает журнал не всей семьи — он переделает то, "
        "что сделал брат"
    )
