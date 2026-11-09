"""Главное меню в боте правит экран один раз, а не дважды.

НАХОДКА (обход всех кнопок бота, 09.11.2026). cb_main выводил меню, а строку
статистики дописывал вторым edit из фоновой задачи. Запоздавшая правка
приходила уже после следующего нажатия: человек жал «🤖 Подключить бота
здесь», а поверх экрана ввода токена на миг вставало главное меню. При
медленной базе вторая правка приходила последней и затирала открытый шаг.

ОБРАТНЫЙ КОНТРОЛЬ: вернуть фоновый `_push_stats` с повторным `_edit` — тест
видит две правки экрана и краснеет.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _SlowPool:
    async def fetchrow(self, *a, **k):
        await asyncio.sleep(0.5)
        return {"today_ops": 3, "active_accs": 1, "in_cooldown": 0, "new_alerts": 0}


class _State:
    async def get_state(self):
        return None

    async def clear(self):
        pass


def _run(monkeypatch, wait):
    from bot.handlers import botmother_menu as bm
    from bot.utils import menu_cache
    from services import infra_pressure

    edits: list[str] = []

    async def fake_edit(cb, text, markup=None):
        edits.append(text)

    async def fake_answer(cb, *a, **k):
        return None

    async def fake_pressure(pool, uid):
        return {}

    monkeypatch.setattr(bm, "_edit", fake_edit)
    monkeypatch.setattr(bm, "safe_answer", fake_answer)
    monkeypatch.setattr(bm, "_MAIN_STATS_WAIT", wait)
    monkeypatch.setattr(infra_pressure, "compute_pressure", fake_pressure)

    uid = 99177001 + int(wait * 10)
    menu_cache.invalidate(f"u:{uid}:")
    cb = SimpleNamespace(from_user=SimpleNamespace(id=uid))

    async def go():
        await bm.cb_main(cb, None, _SlowPool(), _State())
        # дать запоздавшему подсчёту закончиться: правок от него быть не должно
        await asyncio.sleep(0.8)

    asyncio.run(go())
    return edits, menu_cache.get(f"u:{uid}:main_stats", ttl=30.0)


def test_slow_stats_do_not_cause_a_second_edit(monkeypatch):
    edits, cached = _run(monkeypatch, wait=0.05)
    assert len(edits) == 1, f"меню правило экран {len(edits)} раз: {edits}"
    assert "Сегодня" not in edits[0]
    assert cached and "Сегодня: 3" in cached, "подсчёт не доехал в кэш"


def test_fast_stats_are_shown_in_the_single_edit(monkeypatch):
    edits, _ = _run(monkeypatch, wait=2.0)
    assert len(edits) == 1, edits
    assert "Сегодня: 3" in edits[0]
