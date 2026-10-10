"""«Возобновить» возвращает операцию в то же состояние, а не запускает её сейчас.

ЧТО БЫЛО. Возобновление одной операции заодно снимало `scheduled_for`. Это
писалось как быстрый способ поторопить операцию, отложенную живой очередью.
Позже для этого появилась отдельная кнопка «Запустить сейчас»
(run_now_operation), а побочный эффект у «возобновить» остался — и ломал два
сценария сразу.

  1. Запланированное владельцем время пропадало. Операция, заведённая на
     «завтра в 9:00», после паузы и возобновления уходила в работу НЕМЕДЛЕННО:
     пост в реальные каналы в три часа ночи, отменять уже нечего. Владелец
     ничего такого не просил — он нажал «возобновить», а не «запустить сейчас».
  2. Отложенность, которая защищает аккаунты, снималась в обход. Флуд-пауза и
     перенос при занятом флоте живут в том же `scheduled_for`, и пара «пауза →
     возобновить» отправляла аккаунт обратно в Telegram раньше срока, который
     назначил сам Telegram.

Вдобавок две кнопки «возобновить» значили разное: массовая (resume_operations)
время запуска не трогала никогда. Один и тот же жест давал разный результат в
зависимости от того, каким способом его сделали.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _handler_body(name: str) -> str:
    src = inspect.getsource(mini_app_api)
    m = re.search(
        r"    async def " + name + r"\(.*?\n(.*?)\n    async def ", src, re.DOTALL
    )
    assert m, f"обработчик {name} не найден"
    return m.group(1)


def test_single_resume_does_not_clear_the_scheduled_time():
    body = _handler_body("resume_operation")
    assert "status='pending'" in body and "status='paused'" in body, (
        "возобновление перестало быть парой к паузе"
    )
    assert "scheduled_for=NULL" not in body, (
        "«возобновить» снова стирает запланированное время: пост, заведённый на "
        "9:00, уйдёт в каналы немедленно, а флуд-пауза снимется в обход"
    )


def test_bulk_resume_does_not_clear_the_scheduled_time():
    body = _handler_body("resume_operations")
    assert "SET status='pending'" in body and "status='paused'" in body
    assert "scheduled_for" not in body, (
        "массовое возобновление начало трогать время запуска"
    )


def test_both_resume_buttons_mean_the_same_thing():
    """Один жест обязан давать один результат, каким бы способом его ни сделали."""
    one = "scheduled_for=NULL" in _handler_body("resume_operation")
    many = "scheduled_for" in _handler_body("resume_operations")
    assert one == many is False, (
        "кнопки «возобновить» для одной операции и для очереди расходятся в том, "
        "сохраняется ли время запуска"
    )


def test_hurrying_an_operation_is_still_possible_explicitly():
    """Способ поторопить операцию не исчез — он просто стал отдельным действием."""
    body = _handler_body("run_now_operation")
    assert "scheduled_for=NULL" in body, (
        "«Запустить сейчас» перестало снимать время — поторопить операцию нечем"
    )
    assert "status='pending'" in body


def test_run_now_route_is_registered():
    src = inspect.getsource(mini_app_api)
    assert (
        'app.router.add_post("/api/miniapp/operation/{op_id}/run_now", run_now_operation)'
        in src
    ), "без маршрута кнопка «Запустить сейчас» недоступна, и обхода паузы не остаётся"


def test_pause_leaves_the_scheduled_time_alone_too():
    """Пауза тоже не должна трогать время: иначе возвращать было бы нечего."""
    body = _handler_body("pause_operation")
    assert "scheduled_for" not in body
