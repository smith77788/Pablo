"""Сбой чтения цепочки повторов не превращает журнал в пустой.

Двери идемпотентности давно fail-closed: `_journal_fetch` не отдаёт пустой
список на сбое БД, а бросает, и операция уходит на обычный повтор. Но ЧИТАЮТ
они журнал по СЕМЬЕ повторов: у операции, поставленной кнопкой «Повторить»,
свой новый id и пустой журнал, а сделанная работа лежит под id предка. Список
этих id собирался fail-open — «не прочитали цепочку, работаем по своему
журналу». Для повтора «свой журнал» означает «ничего не сделано»:

  • рассылка встала на 203 адресатах из 380, владелец нажал «Повторить» — и
    203 человека получают ВТОРОЕ одинаковое сообщение, самую заметную
    спам-сигнатуру продукта;
  • постинг публикует второй пост в каналы, которые его уже получили;
  • приглашение зовёт второй раз тех, кого уже звало.

Причём дверь при этом отрабатывала честно и ошибки не было вообще: соврал
ключ, по которому она читала. Один сетевой блип к БД стоил ровно этого.
"""
from __future__ import annotations

import asyncio

import pytest

PARENT = 10
CHILD = 11


def _run(coro):
    return asyncio.run(coro)


class _ChainBroken:
    """Цепочка повторов не читается, журнал — читается.

    Ровно та ситуация, в которой fail-open был опасен: журнал жив и содержит
    сделанную предком работу, но без состава семьи дверь прочитала бы его по
    одному id повтора и ничего не нашла.
    """

    def __init__(self) -> None:
        self.chain_reads = 0

    async def fetchrow(self, query, *args):
        self.chain_reads += 1
        raise ConnectionResetError("блип к БД на чтении цепочки")

    async def fetch(self, query, *args):
        if "operation_log" not in query:
            return []
        ids = list(args[0]) if args and args[0] else []
        if PARENT in ids:
            return [{"target": "@ivan"}, {"target": "@olga"}]
        return []


@pytest.mark.parametrize(
    "door", ["completed_targets", "settled_targets", "completed_steps"])
def test_a_door_never_reports_an_empty_journal_on_a_chain_failure(door):
    """Любая дверь идемпотентности откладывает прогон, а не делает всё заново."""
    from services import op_worker

    pool = _ChainBroken()
    try:
        already = _run(getattr(op_worker, door)(pool, CHILD))
    except op_worker.JournalUnreadable:
        assert pool.chain_reads >= 2, (
            "у чтения цепочки должен быть повтор: блип к БД лечится именно им, "
            f"а попыток было {pool.chain_reads}")
        return
    pytest.fail(
        f"{door} вернула «сделано: {already or 'ничего'}» вместо отказа: состав "
        "семьи повторов не прочитан, журнал предка остался непрочитанным, и "
        "повтор пройдёт весь список целей заново — второе сообщение тем, кто "
        "уже получил первое")
