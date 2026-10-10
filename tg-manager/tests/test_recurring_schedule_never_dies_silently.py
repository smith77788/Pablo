"""Расписание, оборвавшееся по технической ошибке, не умирает молча.

ЧТО БЫЛО. `_reschedule_recurring` сам написан про то, что молчаливый обрыв
расписания — худший вид отказа: канал перестаёт наполняться, и владелец узнаёт
об этом через неделю, если узнаёт вообще. Для двух причин он так и работал:
предохранитель и тариф уходят владельцу отдельным сообщением
(`_notify_recurring_stopped`).

А общий `except Exception` — любая другая причина, по которой следующий круг не
встал (сбой БД, отказ шины, битые параметры) — писал ОДНУ строку в лог и
возвращался. Следующего запуска нет, расписание мертво, владельцу не сказано
ничего. То есть ровно тот отказ, против которого функция и написана, оставался
единственным молчаливым.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker


class _Pool:
    async def fetchrow(self, query, *args):
        return {"label": "Автопост", "total_items": 10}

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        return None


def _params():
    return {"repeat_interval_min": 60, "channel": "@c"}


def _run(monkeypatch, failure):
    """Прогнать продление, где постановка следующего круга падает `failure`."""
    told: list[str] = []

    async def _notify(pool, bot, owner_id, op_id, op_type, why):
        told.append(why)

    monkeypatch.setattr(op_worker, "_notify_recurring_stopped", _notify)

    from services import operation_bus as _obus

    async def _boom(*a, **kw):
        raise failure

    monkeypatch.setattr(_obus, "submit", _boom)
    asyncio.run(op_worker._reschedule_recurring(
        _Pool(), None, 51, 555, "mass_publish", _params(), productive=True))
    return told


def test_technical_failure_is_told_to_the_owner(monkeypatch):
    told = _run(monkeypatch, RuntimeError("пул соединений исчерпан"))
    assert told, (
        "следующий круг не поставлен, расписание мертво — и владельцу не сказано "
        "ничего: канал просто перестанет наполняться")
    assert "техническ" in told[0].lower(), (
        f"причина обрыва владельцу непонятна: {told[0]!r}")


def test_owner_text_is_russian(monkeypatch):
    """Владелец не понимает английский — текст причины обязан быть на русском."""
    told = _run(monkeypatch, RuntimeError("connection pool exhausted"))
    import re

    assert told
    assert re.search(r"[а-яА-Я]", told[0]), told[0]
    # текст самой ошибки наружу не выносим: он английский и ничего не объясняет
    assert "connection pool" not in told[0]


def test_named_refusals_still_speak_for_themselves(monkeypatch):
    """Предохранитель и тариф объясняют причину сами, а не «техническая ошибка»."""
    from services import operation_bus as _obus

    told = _run(monkeypatch, _obus.PlanRequiredError("mass_publish", "paid"))
    assert told and "техническ" not in told[0].lower(), (
        f"отказ по тарифу подменён технической ошибкой: {told[0]!r}")


def test_failure_is_counted_in_metrics(monkeypatch):
    """Срабатывание механизма надёжности должно быть видно снаружи, не в логах."""
    from services import metrics

    metrics.reset()
    _run(monkeypatch, RuntimeError("сбой"))
    snap = metrics.snapshot()
    assert any("infragram_recurring_reschedule_failures_total" in str(k)
               for k in (snap.get("counters") or {})), (
        "обрыв расписания не попал в метрики: снаружи он снова невидим")
    assert ("infragram_recurring_reschedule_failures_total"
            in metrics._HELP), "у метрики нет описания — она не попадёт в выдачу"
