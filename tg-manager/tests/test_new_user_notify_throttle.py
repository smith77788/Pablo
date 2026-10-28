"""Анти-накрутка: уведомления «новый подписчик» троттлятся ПО БОТУ.

Регресс: раньше каждый новый юзер слал владельцу отдельный DM через main-bot.
При накрутке ботами поток фейков насыщал main-bot (FloodWait) — и Infragram
переставал отвечать всем. Теперь на бота — не чаще одного уведомления в окно,
с агрегированным счётчиком и предупреждением о возможной накрутке.
"""
from __future__ import annotations

from services import auto_responder as ar


def _reset(bot_id):
    ar._new_user_notify.pop(bot_id, None)


def test_first_subscriber_notified_immediately():
    _reset(9001)
    assert ar._new_user_notify_decide(9001) == 1


def test_burst_is_throttled_to_one_per_window():
    _reset(9002)
    # первый — сразу
    assert ar._new_user_notify_decide(9002) == 1
    # следующие 500 в пределах окна — подавлены (None), но копятся
    suppressed = [ar._new_user_notify_decide(9002) for _ in range(500)]
    assert all(x is None for x in suppressed)
    assert ar._new_user_notify[9002]["count"] == 500
    # окно прошло → следующий отдаёт накопленный счётчик (агрегат > 1)
    ar._new_user_notify[9002]["last"] -= (ar._NEW_USER_NOTIFY_COOLDOWN + 1)
    n = ar._new_user_notify_decide(9002)
    assert n == 501                      # 500 накопленных + текущий
    # и снова копим с нуля
    assert ar._new_user_notify[9002]["count"] == 0


def test_per_bot_isolation():
    _reset(9003); _reset(9004)
    assert ar._new_user_notify_decide(9003) == 1
    # другой бот не делит слот троттлинга
    assert ar._new_user_notify_decide(9004) == 1
