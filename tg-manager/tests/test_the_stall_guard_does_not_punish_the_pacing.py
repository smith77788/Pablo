"""Сторож застоя обрывал прогон за соблюдение собственного анти-бан темпа.

АРИФМЕТИКА РАЗРЫВА. Сторож застоя судит по движению `done_items`: не
двинулось за `_OP_STALL_MIN` (по умолчанию 90 минут, владелец вправе сузить до
10) — прогон прерывается. А самая долгая пауза продукта — между группами у
Growth Agent: `random.uniform(600, 1200)` под губернатором темпа, множитель
которого доходит до 4.0 (`fleet_governor._STEPS`), то есть до 80 минут. И это
только одна пауза: следом идёт вступление, затем пауза перед постом (3–8
минут), и лишь ПОСЛЕ поста двигается `done_items`.

То есть при настройках по умолчанию здоровый прогон под давлением флота
подходит к порогу сторожа, а при флуде на вступлении — переходит его. Дальше
`asyncio.TimeoutError`, возврат в очередь, `done_items=0`, списанная единица
бюджета живучести — и после `_MAX_REVIVES` таких «спасений» здоровая операция
объявляется ядовитой. Снаружи это «операция сама падает», причём тем чаще, чем
осторожнее продукт себя ведёт.

ЧТО ПРОВЕРЯЕМ. Пауза объявляет себя (`_declared_pause_until`), и сторож
считает объявленную паузу движением. Защита при этом не снимается: у
объявления свой дедлайн, поэтому прогон, зависший ПОСЛЕ паузы (или без паузы
вовсе), сторож по-прежнему снимает.
"""
from __future__ import annotations

import asyncio

import pytest

from services import fleet_governor
from services import op_worker


class _Pool:
    """Пул, у которого `done_items` стоит на месте."""

    def __init__(self, done: int = 5):
        self.done = done

    async def fetchrow(self, query, *args):
        return {"done_items": self.done}


@pytest.fixture
def fast(monkeypatch):
    """Те же пределы, только в секундах вместо минут."""
    monkeypatch.setattr(op_worker, "_OP_STALL_POLL_S", 0.02)
    monkeypatch.setattr(op_worker, "_OP_STALL_MIN", 0.05 / 60)   # 50 мс
    monkeypatch.setattr(op_worker, "_CANCEL_PAUSE_STEP_S", 0.01)
    # getattr, а не прямое обращение: без фикса реестра пауз в модуле нет, и
    # тест обязан падать на ПОВЕДЕНИИ сторожа, а не на AttributeError в фикстуре.
    getattr(op_worker, "_declared_pause_until", {}).clear()
    yield
    getattr(op_worker, "_declared_pause_until", {}).clear()


# ── Арифметика, из которой разрыв и следует ──────────────────────────────────

def test_the_longest_pacing_pause_reaches_the_stall_window():
    """Если это перестанет быть правдой — объявление паузы можно упрощать."""
    worst_pause_s = 1200 * max(mult for _thr, mult in fleet_governor._STEPS)
    assert worst_pause_s >= 0.8 * op_worker._OP_STALL_MIN * 60, (
        f"самая долгая пауза {worst_pause_s:.0f}с против окна сторожа "
        f"{op_worker._OP_STALL_MIN * 60:.0f}с — проверьте, не изменились ли "
        "множители губернатора или окно по умолчанию")


# ── Поведение сторожа ────────────────────────────────────────────────────────

def test_a_declared_pause_is_not_a_stall(fast):
    """Иначе прогон снимают за выдержанный анти-бан темп."""
    pool = _Pool()

    async def _work():
        # Пауза вдвое длиннее окна сторожа и ничего не двигает.
        await op_worker._pause_watching_for_cancel(pool, 1, 0.12, "тест")
        return {"status": "done"}

    async def _go():
        return await op_worker._run_with_stall_guard(
            pool, _work(), 1, "niche_growth_post", timeout_s=5.0)

    assert asyncio.run(_go()) == {"status": "done"}


def test_a_hung_run_is_still_stopped(fast):
    """Самопроверка: без объявления сторож обязан снять прогон."""
    pool = _Pool()

    async def _hang():
        await asyncio.sleep(5.0)

    async def _go():
        return await op_worker._run_with_stall_guard(
            pool, _hang(), 2, "niche_growth_post", timeout_s=5.0)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_go())


def test_a_run_that_hangs_after_the_pause_is_stopped(fast):
    """Объявление не бессрочное: дедлайн истёк — сторож снова судит."""
    pool = _Pool()

    async def _pause_then_hang():
        await op_worker._pause_watching_for_cancel(pool, 3, 0.05, "тест")
        await asyncio.sleep(5.0)

    async def _go():
        return await op_worker._run_with_stall_guard(
            pool, _pause_then_hang(), 3, "niche_growth_post", timeout_s=5.0)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_go())


def test_the_pause_cleans_up_after_itself(fast):
    """Иначе одна пауза сняла бы сторожа с операции навсегда."""
    pool = _Pool()
    asyncio.run(op_worker._pause_watching_for_cancel(pool, 4, 0.02, "тест"))
    assert 4 not in getattr(op_worker, "_declared_pause_until", {})


def test_the_pause_still_returns_what_it_slept(fast):
    """Объявление не изменило смысл самой паузы."""
    slept = asyncio.run(
        op_worker._pause_watching_for_cancel(_Pool(), 5, 0.05, "тест"))
    assert slept == pytest.approx(0.05, abs=1e-6)
