"""Регресс: hourly A/B-свип планировщика не должен быть fire-and-forget без ссылки.

`scheduler.run` запускал `asyncio.get_event_loop().create_task(declare_ab_winners(pool))`
без сохранения ссылки. Event loop держит на задачу ТОЛЬКО слабую ссылку (asyncio docs:
«A task that isn't referenced elsewhere may get garbage collected at any time, even
before it's done»). Значит часовой свип объявления победителей A/B мог быть собран GC
до завершения → победители молча не declared. А именно на систему experiments мы
направляем пользователя из виджета рассылок — её надёжность важна.

Фикс: свип ограничен и идёт раз в час перед sleep(60) → ждём напрямую (`await`),
устраняя GC-исчезновение. Тест ловит возврат анти-паттерна.
"""
from __future__ import annotations

import inspect

from services import scheduler


def test_ab_sweep_is_awaited_not_fire_and_forget():
    src = inspect.getsource(scheduler.run)
    assert "await declare_ab_winners(pool)" in src, (
        "часовой A/B-свип должен ждаться напрямую, а не теряться в несохранённом create_task"
    )
    assert "get_event_loop().create_task(declare_ab_winners" not in src, (
        "fire-and-forget create_task без ссылки → GC может собрать задачу до завершения"
    )
    assert "create_task(declare_ab_winners" not in src, (
        "любой create_task(declare_ab_winners) без сохранённой ссылки = GC-риск"
    )
