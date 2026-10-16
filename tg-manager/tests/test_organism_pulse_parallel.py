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
    """14 секций по 0.05с: последовательно ≈0.7с, параллельно ≈0.05с."""
    def _maker(name):
        async def _f(pool, owner_id):
            await asyncio.sleep(0.05)
            return {"name": name}
        return _f

    _patch_all(monkeypatch, _maker)

    t0 = time.monotonic()
    snap = await world.snapshot(object(), 1)
    elapsed = time.monotonic() - t0

    assert set(snap.keys()) == {
        "fleet", "ops", "graph", "vault", "goal", "growth", "seo", "retention",
        "anomalies", "invite_chats", "bots", "chat_warmup", "events_24h", "vlayer",
    }
    assert elapsed < 0.3, (
        f"секции собираются не параллельно: {elapsed:.2f}с на 14×0.05с "
        f"(последовательно было бы ~0.7с) — пульс снова упрётся в таймаут"
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
