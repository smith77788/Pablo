"""Модель состояний операции: недоведённая работа обязана иметь СВОЙ статус.

Разрыв с живого прогона (описан в services/operation_retry.py): массовый инвайт
взял 203 цели из 380 со 177 ошибками и закрылся статусом `done` — владелец
получил зелёную галочку «✅ завершена» на работе, которая не доведена. Правду
приходилось восстанавливать отдельно, по счётчикам, потому что самому статусу
верить было нельзя.

Здесь сторожим: классификация решает по РЕАЛЬНЫМ счётчикам, `partial` —
терминальное состояние, и ни один guard «не затирать завершённую операцию» про
него не забывает.

Тесты на чистой логике (services/op_status без зависимостей) плюс проверка
исходника op_worker — сам модуль импортирует telethon и в тестовой среде не
поднимается, поэтому связку проверяем текстом, как и соседние тесты очереди.
"""
from __future__ import annotations

import os

from services import op_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── Классификация исхода ─────────────────────────────────────────────────────

def test_partial_success_is_not_done():
    """Главный регресс: 203 из 380 со 177 ошибками — это НЕ `done`."""
    assert op_status.classify_final("done", ok=203, failed=177) == op_status.PARTIAL


def test_full_success_is_done():
    assert op_status.classify_final("done", ok=380, failed=0) == op_status.DONE


def test_all_targets_failed_is_failed():
    assert op_status.classify_final("done", ok=0, failed=380) == op_status.FAILED


def test_handler_refusal_before_any_work_is_failed():
    """«Нет активных аккаунтов»: исполнитель отказал ДО работы — это провал."""
    assert op_status.classify_final("failed", ok=0, failed=0) == op_status.FAILED


def test_handler_aborted_after_partial_work_is_partial():
    """Исполнитель взял часть целей и оборвался — не `failed`: работа была."""
    assert op_status.classify_final("failed", ok=12, failed=0) == op_status.PARTIAL


def test_progress_undershoot_is_partial_even_without_error_counter():
    """Исполнитель остановился раньше и НЕ записал остаток в failed.

    Ровно этот случай закрывался зелёным `done` с недобранной целью: счётчик
    ошибок пуст, значит по ok/failed операция «чистая», а до цели не дошли.
    """
    assert op_status.classify_final(
        "done", ok=203, failed=0, done_items=203, total_items=380
    ) == op_status.PARTIAL


def test_progress_reached_target_is_done():
    assert op_status.classify_final(
        "done", ok=380, failed=0, done_items=380, total_items=380
    ) == op_status.DONE


def test_service_op_without_targets_is_done():
    """Обслуживающая операция без целей (нечего делать) — честное «выполнено»."""
    assert op_status.classify_final("done", ok=0, failed=0) == op_status.DONE


def test_cancelled_is_not_overwritten_by_counters():
    """Отмену нельзя затереть счётчиками — иначе отменённая операция «успешна»."""
    assert op_status.classify_final("cancelled", ok=5, failed=1) == op_status.CANCELLED


def test_requeue_is_passed_through():
    """`requeue` решается выше по стеку и не должен превращаться в терминальный."""
    assert op_status.classify_final("requeue", ok=0, failed=0) == "requeue"


def test_counters_survive_garbage_input():
    """Счётчики приходят из JSON исполнителей — мусор не должен ронять классификацию."""
    assert op_status.classify_final("done", ok="203", failed="177") == op_status.PARTIAL
    assert op_status.classify_final("done", ok=None, failed=None) == op_status.DONE


# ── Свойства состояний ───────────────────────────────────────────────────────

def test_partial_is_terminal():
    """Иначе guard'ы `status NOT IN (...)` считают операцию живой и затирают исход."""
    assert op_status.is_terminal(op_status.PARTIAL)
    assert not op_status.is_in_flight(op_status.PARTIAL)


def test_partial_is_productive_but_not_successful():
    """Работа была (предохранитель не трогаем), но доведённой её звать нельзя."""
    assert op_status.is_productive(op_status.PARTIAL)
    assert op_status.PARTIAL not in op_status.SUCCESSFUL


def test_failed_and_cancelled_are_not_productive():
    assert not op_status.is_productive(op_status.FAILED)
    assert not op_status.is_productive(op_status.CANCELLED)


def test_sql_terminal_list_includes_partial():
    """Строка уходит в SQL-guard'ы; без partial они пропускают завершённую op."""
    sql = op_status.sql_terminal_list()
    for st in ("done", "partial", "failed", "cancelled"):
        assert f"'{st}'" in sql, sql
    assert sql.startswith("(") and sql.endswith(")")


def test_partial_has_its_own_icon_and_label():
    """Владелец не должен видеть зелёную галочку на недоведённой работе."""
    assert op_status.icon(op_status.PARTIAL) != op_status.icon(op_status.DONE)
    assert op_status.label(op_status.PARTIAL) != op_status.label(op_status.DONE)


# ── Связка с воркером (по исходнику: op_worker тянет telethon) ────────────────

def test_worker_uses_classifier_not_handwritten_branches():
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    assert "op_status.classify_final(" in body, (
        "финальный статус обязан считаться моделью состояний, а не ветвлением на месте"
    )
    # Старое ветвление «всё, что не failed → done» не должно вернуться.
    assert '_final_status = "done"' not in body


def test_worker_terminal_guards_know_about_partial():
    """Запоздавший апдейт не должен переписывать УЖЕ завершённую операцию."""
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    assert "NOT IN ('done','failed','cancelled')" not in body, (
        "рукописный список терминальных статусов забывает partial — "
        "используйте op_status.sql_terminal_list()"
    )
    assert "op_status.sql_terminal_list()" in body


def test_worker_result_status_matches_queue_status():
    """result['status'] и operation_queue.status читают разные экраны.

    Разъехавшись, они показывали разный исход одной и той же операции.
    """
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    assert 'result["status"] = _final_status' in body


def test_partial_does_not_open_circuit_breaker():
    """Партиал — доказательство, что Telegram нас пускает, а не повод глушить всё.

    Массовым операциям (инвайт, DM) терять часть целей положено по их природе:
    считая партиал сбоем, предохранитель останавливал бы ровно их.
    """
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    assert "_circuit_breaker_record(owner_id, op_status.is_productive(_final_status))" in body


def test_partial_keeps_recurring_schedule_alive():
    """Один сбойный канал в серии не должен навсегда обрывать автопостинг."""
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    assert "if op_status.is_productive(_final_status) and op_type in _RECURRING_OK_OPS:" in body


def test_partial_fills_error_msg():
    """«Завершена частично» без причины — тупик: экран показывает статус и молчит."""
    ow = _read("services/op_worker.py")
    body = ow[ow.index("async def _run_op_task"):ow.index("async def _exec_bulk_bot_edit")]
    assert 'if _final_status in ("failed", op_status.PARTIAL):' in body


def test_retry_offered_for_partial():
    """Кнопка повтора обязана появляться на partial — иначе работу нечем доделать."""
    from services.operation_retry import can_retry

    ok, why = can_retry("partial", done_items=203, total_items=380, err_count=177)
    assert ok
    assert "203" in why and "380" in why


def test_bot_status_maps_render_partial():
    """Без записи в карте партиал отрисовывался бы «❓» рядом с числом целей."""
    for rel in (
        "bot/handlers/mass_ops.py",
        "bot/handlers/mass_publish.py",
        "bot/handlers/channel_ops.py",
        "bot/handlers/growth_hub.py",
        "bot/handlers/accounts.py",
    ):
        assert '"partial"' in _read(rel), f"{rel}: статус partial нечем отрисовать"


def test_miniapp_stops_polling_on_partial():
    """Иначе завершённая операция висела бы в опросе и врала «ещё выполняется»."""
    html = _read("mini_app/index.html")
    poll = html[html.index("async function pollOpResult"):]
    poll = poll[:poll.index("function opCanRetry")]
    assert "d.status==='partial'" in poll
