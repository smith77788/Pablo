"""Рассылка, которую не получил никто, не должна рапортовать «выполнено».

Найдено прогоном e2e-набора на живом Postgres: эти тесты пропускаются без
INFRAGRAM_TEST_DSN, поэтому падение никто не видел.

Получатели, заблокировавшие бота или удалённые, идут в «пропущено», а не в
«ошибки». Само по себе это верно: они недостижимы навсегда, повтор бессмысленен,
и на предохранитель флота это не тянет. Но исполнитель из-за этого возвращал
`done` даже когда ok=0: в счётчиках честное «✅ 0/3», а бейдж операции зелёный
«выполнено». Класс «молчаливый итог», который продукт ловит в других местах.

Здесь — решение о статусе отдельной чистой функцией, чтобы регрессию ловил
обычный прогон, а не только стенд с живой базой.
"""
from __future__ import annotations

from services.op_worker import _blast_final_status


def test_nobody_received_is_not_done():
    """Все получатели пропущены (заблокировали бота) → честный провал."""
    assert _blast_final_status(ok_count=0, total=3, cancelled=False) == "failed", (
        "рассылка, которую не получил никто, не может быть «выполнено»")


def test_partial_delivery_is_done():
    """Дошло хоть кому-то — операция сделала своё дело."""
    assert _blast_final_status(ok_count=1, total=3, cancelled=False) == "done"
    assert _blast_final_status(ok_count=3, total=3, cancelled=False) == "done"


def test_no_targets_is_done():
    """Целей не было — делать нечего, это не провал."""
    assert _blast_final_status(ok_count=0, total=0, cancelled=False) == "done"


def test_cancel_wins_over_everything():
    """Остановил владелец — статус про отмену, а не про доставку."""
    assert _blast_final_status(ok_count=0, total=3, cancelled=True) == "cancelled"
    assert _blast_final_status(ok_count=3, total=3, cancelled=True) == "cancelled"


def test_executor_uses_the_shared_decision():
    """Исполнитель обязан звать эту функцию, а не решать статус на месте."""
    import inspect
    from services import op_worker

    src = inspect.getsource(op_worker._exec_self_promo_blast)
    assert "_blast_final_status(" in src, (
        "исполнитель снова решает статус сам — регрессия перестанет ловиться")
    assert '"status": "cancelled" if cancelled else "done"' not in src, (
        "вернулся прежний статус, игнорирующий нулевую доставку")
