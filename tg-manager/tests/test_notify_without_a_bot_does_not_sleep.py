"""Уведомление без объекта бота не спит в круге повторов.

ЗАЧЕМ. `notify_if_enabled` повторяет временный сбой доставки с паузами
1 с и 4 с. Вызов без бота (`bot=None` — так кампанию рассылки зовут из
процессов и тестов, где бота нет) падал на `None.send_message` с
AttributeError, который круг повторов принимал за временный сбой: 5 с сна на
каждое уведомление. На наборе тестов это стоило 20 с на каждый из шести
прогонов кампании, на проде — те же секунды в цикле рассылки.

Исход обязан остаться прежним: False («временный сбой») и запись в лог.
"""
from __future__ import annotations

import asyncio

from database import db


class _Pool:
    async def fetchrow(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return "OK"

    async def fetchval(self, *a, **k):
        return None


def test_no_bot_means_no_retry_sleep(monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds, *a, **k):
        slept.append(float(seconds))

    async def _settings(pool, user_id):
        return {}

    monkeypatch.setattr(db, "get_notification_settings", _settings)
    monkeypatch.setattr(db.asyncio, "sleep", _sleep)
    db._notify_cooldown.clear()
    ok = asyncio.run(db.notify_if_enabled(_Pool(), None, 1, "op_done", "текст"))
    assert ok is False, "исход прежний: временный сбой"
    assert slept == [], f"без бота повторять нечего, а спали {slept}"
