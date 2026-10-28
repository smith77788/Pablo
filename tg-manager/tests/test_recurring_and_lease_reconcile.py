"""Расписание не сильнее защиты, а реконсилер не отбирает чужие сессии.

ДВА КЛАССА.

1. РАСПИСАНИЕ ОБХОДИЛО ПРЕДОХРАНИТЕЛЬ. Рекуррентные операции (автопостинг)
   ставили следующий круг прямым `INSERT INTO operation_queue` из исполнителя.
   Мимо шины — значит мимо предохранителя Ban Weather, который приостанавливает
   тип операции ровно тогда, когда тот прямо сейчас массово убивает аккаунты
   владельца, и мимо гейта тарифа: автопостинг, заведённый на платном плане,
   продолжал крутиться вечно после отмены подписки. Защита, которую можно
   обойти расписанием, не защищает ничего.

2. РЕКОНСИЛЕР ОТБИРАЛ ЖИВЫЕ СЕССИИ. `_reconcile_in_operation` снимал
   `in_operation` со всего, чего нет в памяти ЭТОГО процесса. С одной репликой
   это верно, со второй (роль web + worker разносится штатно) он снимал защиту
   с сессий, которые прямо сейчас держит сосед: обе реплики считали аккаунт
   свободным и открывали одну сессию дважды — AUTH_KEY_DUPLICATED, то есть
   мёртвый аккаунт. `_db_release` и `reset_stale_in_operation` аренду уважают,
   реконсилер писал в ту же колонку и не уважал.
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _op_worker_src() -> str:
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        return f.read()


# ── Рекуррентные операции ────────────────────────────────────────────────────

def test_recurring_reschedule_goes_through_the_bus():
    src = _op_worker_src()
    assert "INSERT INTO operation_queue" not in src, (
        "исполнитель снова ставит операцию мимо шины — расписание обходит "
        "предохранитель Ban Weather и гейт тарифа"
    )
    # Якорь — сам помощник продления: раньше блок стоял прямо в _run_op_task,
    # и условие читалось как «op_type in _RECURRING_OK_OPS».
    idx = src.index("async def _reschedule_recurring")
    seg = src[idx:idx + 8000]
    assert "submit(" in seg, "переочередь рекуррентной операции не идёт через шину"


def test_recurring_reschedule_handles_a_refusal_from_the_bus():
    """Отказ шины — это ответ, а не сбой: круг пропускаем и говорим владельцу."""
    src = _op_worker_src()
    # Якорь — сам помощник продления: раньше блок стоял прямо в _run_op_task,
    # и условие читалось как «op_type in _RECURRING_OK_OPS».
    idx = src.index("async def _reschedule_recurring")
    seg = src[idx:idx + 8000]
    assert "ImmunityBlockedError" in seg, (
        "отказ предохранителя не разобран отдельно — уйдёт в общий except как "
        "«reschedule failed», и владелец узнает об оборванном расписании по "
        "тишине в канале"
    )
    assert "PlanRequiredError" in seg
    assert "_notify_recurring_stopped" in seg, (
        "расписание обрывается молча — это худший вид отказа"
    )


def test_repeat_marker_is_not_appended_forever():
    """Метка повтора ставится один раз, а не копится кругами («Пост ↻↻↻↻»)."""
    src = _op_worker_src()
    # Якорь — сам помощник продления: раньше блок стоял прямо в _run_op_task,
    # и условие читалось как «op_type in _RECURRING_OK_OPS».
    idx = src.index("async def _reschedule_recurring")
    seg = src[idx:idx + 8000]
    assert 'endswith(" ↻")' in seg


# ── Аренда аккаунтов ─────────────────────────────────────────────────────────

class _RecordingPool:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "UPDATE 0"


@pytest.mark.asyncio
async def test_reconcile_never_clears_a_live_foreign_lease():
    from services import op_worker

    pool = _RecordingPool()
    await op_worker._reconcile_in_operation(pool)

    assert pool.queries, "реконсилер не сходил в БД вовсе"
    query, args = pool.queries[0]
    assert "op_lease_owner" in query, (
        "реконсилер снимает in_operation, не глядя на аренду — со второй "
        "репликой он отберёт чужую живую сессию (AUTH_KEY_DUPLICATED)"
    )
    assert "op_lease_until < now()" in query, (
        "нет условия на истёкшую аренду — реконсилер перестанет чистить "
        "собственных зомби, ради которых он и существует"
    )
    assert op_worker._WORKER_ID in args, (
        "свой идентификатор не передан — своя же аренда не будет распознана"
    )


def test_reconcile_and_release_use_the_same_lease_predicate():
    """Две функции пишут в одну колонку — расхождение предикатов и есть баг."""
    src = _op_worker_src()
    for fn in ("_db_release", "_reconcile_in_operation", "reset_stale_in_operation"):
        idx = src.index(f"async def {fn}(")
        seg = src[idx:idx + 2500]
        assert "op_lease_owner" in seg, f"{fn} снимает занятость мимо аренды"
