"""Операция не должна стартовать второй раз, пока первая задача её доигрывает.

Разрыв. Задача возвращает операцию в очередь ИЗНУТРИ себя — отсрочка по флуду,
нехватка аккаунтов, открытая цепь предохранителя. Все три ставят
`status='pending'` и делают `return`, а освобождение аккаунтов и выписка из
`_active_op_ids` происходят позже, в `finally`. Между этими моментами есть
await'ы — уведомление владельцу уходит в сеть, — то есть окно в сотни
миллисекунд, а то и секунды.

Поллер крутится в том же цикле раз в 10 секунд и видел такую операцию как
обычную ожидающую. Дальше ломалось всё подряд:

  * `finally` СТАРОЙ задачи освобождал аккаунты под НОВЫМ прогоном —
    `in_operation=FALSE` на живых сессиях, которые тут же подхватывали
    прогрев/призрак/ротация. Это AUTH_KEY_DUPLICATED, то есть мёртвый аккаунт;
  * тот же `finally` выписывал из `_active_op_ids` чужой теперь op_id, после
    чего сторож зависших считал новый прогон брошенным и сбрасывал его по
    таймауту, а алерт слал владельцу ложное «операция застряла».

Закрывается на входе: пока задача не отпустила операцию, она не кандидат.

op_worker импортирует telethon и в тестовой среде не поднимается — проверяем
исходником, как и соседние тесты очереди.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"async def {name}")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


def _run_op_task_body(src: str) -> str:
    """_run_op_task целиком.

    Обычная нарезка «до следующего def верхнего уровня» здесь не годится:
    внутри функции объявлены вложенные помощники. Границу берём по первому
    исполнителю, как и соседние тесты очереди.
    """
    return src[src.index("async def _run_op_task"):src.index("async def _exec_bulk_bot_edit")]


def test_poller_excludes_ops_this_process_still_holds():
    body = _fn(_read("services/op_worker.py"), "_process_pending")
    assert "oq.id != ALL($4::bigint[])" in body, (
        "поллер обязан пропускать операции, которые эта задача ещё доигрывает"
    )
    assert "active_now" in body


def test_active_set_is_read_under_the_lock():
    """Снимок без блокировки — та же гонка, только на уровне множества."""
    body = _fn(_read("services/op_worker.py"), "_process_pending")
    seg = body[:body.index("available_slots = ")]
    assert "async with _active_lock:" in seg
    assert "frozenset(_active_op_ids)" in body


def test_active_set_is_passed_as_a_parameter_not_interpolated():
    """Список id в текст запроса не склеивается — это и инъекция, и отказ плана."""
    body = _fn(_read("services/op_worker.py"), "_process_pending")
    assert "(list(active_now) or None)" in body
    assert "f\"\"\"WITH" not in body, "запрос не должен быть f-строкой"


def test_empty_active_set_does_not_block_the_queue():
    """NULL вместо пустого массива: иначе `!= ALL('{}')` вело бы себя неожиданно.

    Пустой список обязан превращаться в None, и условие тогда пропускает всё.
    """
    body = _fn(_read("services/op_worker.py"), "_process_pending")
    assert "$4::bigint[] IS NULL OR" in body, (
        "при пустом наборе активных условие обязано отключаться целиком"
    )
    # Тот же приём уже используется сторожем зависших — держим единообразно.
    watchdog = _fn(_read("services/op_worker.py"), "_watchdog_stale")
    assert "$2::bigint[] IS NULL OR" in watchdog


def test_slots_are_counted_from_the_same_snapshot():
    """Число свободных слотов и фильтр обязаны быть согласованы.

    Иначе поллер посчитает слоты по одному состоянию, а отфильтрует по другому.
    """
    body = _fn(_read("services/op_worker.py"), "_process_pending")
    assert "available_slots = _MAX_PARALLEL - len(active_now)" in body


def test_all_three_requeue_paths_still_return_to_pending():
    """Гонку закрываем фильтром, а не отменой возврата в очередь.

    Если какой-то из путей перестанет возвращать операцию в pending, она
    зависнет в running до сторожа — это другая, ранее закрытая беда.
    """
    ow = _read("services/op_worker.py")
    for name in ("_requeue_op_no_accounts", "_defer_op_for_flood", "_release_op_for_circuit"):
        body = _fn(ow, name)
        assert "status='pending'" in body, f"{name} обязан возвращать операцию в очередь"


def test_cleanup_still_happens_in_finally():
    """Фильтр помогает только пока освобождение реально происходит."""
    body = _run_op_task_body(_read("services/op_worker.py"))
    fin = body[body.rindex("finally:"):]
    assert "release_operation_accounts(op_id)" in fin
    assert "_active_op_ids.discard(op_id)" in fin


def test_requeue_paths_return_before_cleanup_so_the_window_is_real():
    """Сторож самого теста: если порядок изменят, проверка выше станет бессмысленной.

    Возврат в очередь обязан идти ДО finally — именно это и создаёт окно,
    которое закрывает фильтр. Если однажды порядок поменяется, пусть тест
    скажет об этом, а не молчит.
    """
    body = _run_op_task_body(_read("services/op_worker.py"))
    requeue_at = body.index('if result.get("status") == "requeue":')
    finally_at = body.rindex("finally:")
    assert requeue_at < finally_at
