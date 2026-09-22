"""Autopost v2 — рекуррентные посты в свои каналы (P0 Operator Top-1 Roadmap).

Recurring раньше был только у managed-бот рассылок (scheduled_broadcasts).
Теперь operation_queue-постинг: repeat_interval_min>0 → op_worker переочередит
следующий запуск. Только постинг-op'ы (allowlist), аккаунт-действия — нет.
"""
from __future__ import annotations

import inspect

from services import op_worker
from services import mini_app_api


def test_allowlist_has_posting_ops_only():
    ok = op_worker._RECURRING_OK_OPS
    assert "quick_post" in ok and "mass_publish" in ok and "run_broadcast" in ok
    # аккаунт-действия НЕ должны рекуррентно зацикливаться
    for danger in ("leave_all_chats", "delete_private_dialogs", "mass_invite",
                   "profile_setter", "delete_contacts", "read_all_dialogs"):
        assert danger not in ok, f"{danger} не должен быть в _RECURRING_OK_OPS"


def test_op_worker_reschedules_on_done():
    src = inspect.getsource(op_worker)
    # Переочередь гейтится результативностью + allowlist + repeat_interval_min.
    # Раньше гейт был `_final_status == "done"`, и один сбойный канал в серии
    # НАВСЕГДА обрывал автопостинг: итерация закрывалась не «done», следующая
    # не ставилась, и владелец узнавал об этом только по тишине в канале.
    # Результативность прогона теперь ЕДЕТ В ПОМОЩНИКА параметром: он решает,
    # продлевать ли расписание после неудачного круга (см.
    # tests/test_recurring_schedule_survives_a_bad_round.py).
    assert "productive=op_status.is_productive(_final_status)" in src
    assert "op_type not in _RECURRING_OK_OPS" in src
    assert "repeat_interval_min" in src
    # Реально ставит новый op со сдвигом времени — ЧЕРЕЗ ШИНУ.
    # Раньше здесь стоял прямой INSERT с `make_interval(mins => $6)`, и это
    # означало, что расписание сильнее защиты: круг заводился даже тогда, когда
    # предохранитель Ban Weather приостановил этот тип операций, а автопостинг
    # на платном плане продолжал крутиться после отмены подписки. Проверяем
    # смысл (следующий круг ставится со сдвигом на repeat_interval_min), а не
    # конкретный SQL: механизм теперь общий для всего продукта.
    seg = src[src.index("async def _reschedule_recurring"):][:8000]
    assert "submit(" in seg, "переочередь идёт мимо operation_bus"
    assert "timedelta(minutes=_rmin)" in seg, "следующий круг ставится без сдвига времени"
    # repeat_count декрементится (ограничение числа повторов)
    assert "repeat_count" in src


def test_endpoints_accept_repeat_interval():
    src = inspect.getsource(mini_app_api)
    # оба постинг-эндпойнта принимают repeat_interval_min
    assert src.count("repeat_interval_min") >= 2
    assert '"repeat_interval_min"' in src or "'repeat_interval_min'" in src


def test_ui_has_repeat_control():
    from pathlib import Path
    html = (Path(op_worker.__file__).resolve().parents[1] / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'id="qpRepeat"' in html
    assert "payload.repeat_interval_min=repMin" in html


def test_partial_iteration_keeps_schedule_alive():
    """Частично выполненная итерация обязана продлевать расписание."""
    from services import op_status
    assert op_status.is_productive(op_status.PARTIAL), (
        "иначе один сбойный канал в серии навсегда обрывает автопостинг"
    )
    assert not op_status.is_productive(op_status.FAILED), (
        "полностью провалившаяся итерация расписание продлевать не должна"
    )
