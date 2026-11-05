"""Пульс организма собирается параллельно и переживает зависшую секцию.

Жалоба владельца: «Пульс не отвечает» — на крупном флоте (десятки ботов и
аккаунтов, сотни каналов) сводка не успевала за клиентский таймаут 30с.

Причина: world.snapshot собирал 14 секций ПОСЛЕДОВАТЕЛЬНО, и суммарное время
десятков запросов к БД переваливало за 30с. Секции независимы и fail-soft,
поэтому их собирают параллельно (время ≈ самой долгой, а не сумма), а зависшую
секцию ограничивает потолок — лучше пульс без одной секции, чем пустой экран.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from services.organism import world

_SECTION_NAMES = (
    "_fleet", "_ops", "_graph", "_vault", "_goal", "_growth", "_seo",
    "_retention", "_anomalies", "_invite_chats", "_bots", "_chat_warmup",
    "_events", "_vlayer",
)


def _patch_all(monkeypatch, maker):
    for name in _SECTION_NAMES:
        monkeypatch.setattr(world, name, maker(name))


@pytest.mark.asyncio
async def test_sections_run_in_parallel(monkeypatch):
    """Параллельность мерим ПЕРЕКРЫТИЕМ, а не настенными часами.

    Прежняя редакция требовала уложиться в 0.3с на 14×0.05с. Это утверждение
    ложно по построению: в CI рядом идут ещё три части прогона, пробуждение
    `asyncio.sleep` опаздывает на десятки миллисекунд, и тест падал «0.58с»
    при полностью исправном `gather` — то есть красил ветку, не называя беды.

    Признак параллельности — сколько секций работало ОДНОВРЕМЕННО: у gather
    пик равен четырнадцати, у последовательного сбора единице. От загрузки
    машины это не зависит вовсе.

    Настенное время осталось, но ТОЛЬКО как сторож от зависания, и предел у
    него соответствующий: он ловит «пульс встал», а не «машина занята».
    """
    now = 0
    peak = 0

    def _maker(name):
        async def _f(pool, owner_id):
            nonlocal now, peak
            now += 1
            peak = max(peak, now)
            try:
                await asyncio.sleep(0.05)
            finally:
                now -= 1
            return {"name": name}
        return _f

    _patch_all(monkeypatch, _maker)

    # Если тест когда-нибудь снова начнёт вести себя странно, надо знать, ЧТО
    # держит цикл. Чужая спящая задача объяснить опоздание не может: сны
    # перекрываются. А вот живой поток, удерживающий GIL, или блокирующий
    # вызов внутри чужой корутины объясняет опоздание пробуждений точно —
    # ровно тот класс, про который services/loop_lag.py. Поэтому снимаем и
    # задачи, и ПОТОКИ, до замера, и печатаем только при неудаче.
    alien = [t.get_name() for t in asyncio.all_tasks()
             if t is not asyncio.current_task() and not t.done()]
    threads = [t.name for t in threading.enumerate()
               if t is not threading.main_thread() and t.is_alive()]

    t0 = time.monotonic()
    snap = await world.snapshot(object(), 1)
    elapsed = time.monotonic() - t0

    assert set(snap.keys()) == {
        "fleet", "ops", "graph", "vault", "goal", "growth", "seo", "retention",
        "anomalies", "invite_chats", "bots", "chat_warmup", "events_24h", "vlayer",
    }
    assert peak == len(_SECTION_NAMES), (
        f"одновременно работало не больше {peak} секций из "
        f"{len(_SECTION_NAMES)} — пульс собирается по очереди и снова упрётся "
        f"в таймаут клиента"
    )
    assert elapsed < 5.0, (
        f"пульс встал: {elapsed:.1f}с на 14×0.05с при полном перекрытии. "
        f"Посторонние задачи на цикле: {alien[:8] or 'нет'}; "
        f"живые потоки: {threads[:8] or 'нет'}"
    )


@pytest.mark.asyncio
async def test_hung_section_does_not_sink_the_pulse(monkeypatch):
    """Одна зависшая секция ограничивается потолком, остальные доезжают."""
    monkeypatch.setattr(world, "_SECTION_TIMEOUT_S", 0.1)

    def _maker(name):
        async def _f(pool, owner_id):
            if name == "_seo":
                await asyncio.sleep(10)  # «зависла» подсистема
            return {"name": name}
        return _f

    _patch_all(monkeypatch, _maker)

    t0 = time.monotonic()
    snap = await world.snapshot(object(), 1)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"зависшая секция держала весь пульс: {elapsed:.2f}с"
    assert snap["seo"] == {}, "зависшая секция должна деградировать в пустую"
    assert snap["fleet"] == {"name": "_fleet"}, "здоровые секции должны доехать"


@pytest.mark.asyncio
async def test_failing_section_degrades_to_empty(monkeypatch):
    """Падение одной секции не роняет весь snapshot."""
    def _maker(name):
        async def _f(pool, owner_id):
            if name == "_graph":
                raise RuntimeError("подсистема сломана")
            return {"name": name}
        return _f

    _patch_all(monkeypatch, _maker)

    snap = await world.snapshot(object(), 1)
    assert snap["graph"] == {}, "упавшая секция должна стать пустой, а не падать"
    assert snap["ops"] == {"name": "_ops"}
