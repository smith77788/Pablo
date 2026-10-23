"""Разбор инвайтинга не должен опрашивать флот по одному аккаунту.

ЧТО БЫЛО СЛОМАНО (производительность). На каждый аккаунт делались ДВЕ
последовательные проверки — карантин риск-пульса и остаток суточного лимита. На
флоте в 200 аккаунтов это 400 round-trip'ов подряд в ИНТЕРАКТИВНОМ экране:
секунды ожидания на ровном месте, причём ровно там, куда пользователь приходит
разобраться, почему инвайт идёт плохо.

Порядок результатов обязан сохраняться: рекомендации перечисляют аккаунты, и
перестановка сбивала бы сравнение с прошлым открытием экрана.
"""
from __future__ import annotations

import asyncio

from services import invite_advisor as adv


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_probes_run_concurrently_not_one_by_one():
    live = 0
    peak = 0

    async def _probe(x):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        return x * 2

    res = _run(adv._map_bounded(list(range(50)), _probe))
    assert peak > 1, "проверки шли строго по одной — экран будет ждать секундами"
    assert peak <= adv._PROBE_CONCURRENCY, (
        f"параллелизм не ограничен ({peak}) — разбор займёт весь пул соединений"
    )
    assert res == [i * 2 for i in range(50)], "порядок результатов нарушен"


def test_empty_input_does_nothing():
    assert _run(adv._map_bounded([], lambda x: x)) == []


def test_single_item_still_works():
    async def _probe(x):
        return x + 1
    assert _run(adv._map_bounded([41], _probe)) == [42]


def test_probe_failure_propagates_not_silently_dropped():
    """Сбой проверки не должен превращаться в «аккаунт здоров»: вызывающий код
    ловит исключение и честно помечает раздел недоступным."""
    async def _boom(x):
        if x == 3:
            raise RuntimeError("БД недоступна")
        return x

    try:
        _run(adv._map_bounded([1, 2, 3], _boom))
    except RuntimeError:
        return
    raise AssertionError("сбой проверки был проглочен")
