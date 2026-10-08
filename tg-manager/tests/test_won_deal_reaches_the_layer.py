"""Выигранная сделка обязана доходить до виртуального слоя.

ЧТО БЫЛО. Стадию контакта меняют ДВЕ двери: сенсор намерений (по тексту
входящего сообщения) и экран контакта в мини-аппе (`crm_engine.upsert_crm`).
Слою сообщала только первая, и у неё был свой список стадий — без «won».

То есть о продаже слой не узнавал НИКАК. Человек оставался «Готов купить»,
распадом съезжал в «Интерес» и попадал в список «кого дожимать первыми»:
дожимать предлагалось того, кто уже заплатил. Терминальное «Купил» было
недостижимо (а оно единственное, что по замыслу не распадается), и виртуальное
событие purchase_confirmed не рождалось ни разу — реагировать на продажу
автоматизациям было нечем.

ЧТО ТЕПЕРЬ. Маппинг «стадия → сигнал» один для обеих дверей
(`virtual_layer.STAGE_SIGNAL`), ручная смена стадии его подаёт, и «заплатил»
сильнее прежнего «Потерян»: человека записали в потерянные, он вернулся и
купил — это факт, а не вывод.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from services import virtual_layer as V
from services.contacts_hub import crm_engine

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


# ── Маппинг один на обе двери ──────────────────────────────────────────────

def test_every_crm_stage_maps_to_a_signal():
    """Стадия без сигнала — это стадия, о которой слой не узнаёт."""
    from services.intent_sensor import VALID_STAGES
    for stage in VALID_STAGES:
        assert stage in V.STAGE_SIGNAL, f"стадия {stage} слою не сообщается"
        assert V.STAGE_SIGNAL[stage] in V.SIGNAL_TARGET or \
            V.STAGE_SIGNAL[stage] in V.NEGATIVE_SIGNALS, (
            f"сигнал {V.STAGE_SIGNAL[stage]} слой не знает — стадия {stage} "
            "ничего не изменит")


def test_won_lands_on_the_terminal_rung():
    ch = V.apply_signal({"value": "ready", "confidence": 0.7,
                         "expires_at": T0 + timedelta(hours=5)},
                        V.STAGE_SIGNAL["won"], confidence=0.8, now=T0)
    assert ch["value"] == "purchased", ch
    assert ch["expires_at"] is None, "«Купил» не должен распадаться"


def test_a_buyer_never_decays_back_into_the_chase_list():
    """Иначе исправленный распад сведёт купившего в «кого дожимать»."""
    bought = {"value": "purchased", "confidence": 0.9, "expires_at": None}
    assert V.decay(bought, now=T0 + timedelta(days=365)) is None
    assert V.effective(bought, now=T0 + timedelta(days=365))["value"] == "purchased"


def test_payment_beats_a_previous_lost():
    """Записали в потерянные, человек вернулся и купил. Это факт, не вывод."""
    lost = {"value": V.LOST, "confidence": 0.9, "expires_at": None}
    ch = V.apply_signal(lost, "paid", confidence=0.8, now=T0)
    assert ch and ch["value"] == "purchased", ch


def test_nothing_else_pulls_a_contact_out_of_lost():
    lost = {"value": V.LOST, "confidence": 0.9, "expires_at": None}
    for sig in ("replied", "asked_price", "clicked_offer", "asked_how_to_pay"):
        assert V.apply_signal(lost, sig, now=T0) is None, sig


def test_a_repeat_payment_changes_nothing():
    bought = {"value": "purchased", "confidence": 0.9, "expires_at": None}
    assert V.apply_signal(bought, "paid", now=T0) is None


def test_the_purchase_fires_its_virtual_event():
    assert V.virtual_event_for("ready", "purchased") == "purchase_confirmed"
    assert "purchase_confirmed" in V.VIRTUAL_EVENT_KINDS


# ── Ручная дверь ───────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, existing_stage=None):
        self.existing_stage = existing_stage
        self.executed: list[str] = []

    async def fetchrow(self, sql, *args):
        if "contact_crm" in sql and self.existing_stage is not None:
            return {"stage": self.existing_stage, "deal_value": 0,
                    "currency": "USD", "custom_fields": "{}"}
        return None

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "UPDATE 1"


async def test_manual_stage_change_signals_the_layer(monkeypatch):
    seen: list[tuple] = []

    async def _sig(pool, owner_id, etype, eid, name, **kw):
        seen.append((eid, name, kw.get("source")))
        return {"value": "purchased"}

    monkeypatch.setattr(V, "signal", _sig)
    await crm_engine.upsert_crm(_Pool("negotiation"), 42, "c-1", {"stage": "won"})
    assert seen == [("c-1", "paid", "crm_manual")], (
        "ручная смена стадии слою не сообщается: владелец перетащил человека в "
        "«Выиграно», а слой продолжает считать его «Готов купить»")


async def test_unchanged_stage_is_quiet(monkeypatch):
    seen = []

    async def _sig(*a, **kw):
        seen.append(a)
        return None

    monkeypatch.setattr(V, "signal", _sig)
    await crm_engine.upsert_crm(_Pool("won"), 42, "c-1", {"stage": "won"})
    assert seen == [], "сигнал на несменившуюся стадию — лишний шум в истории"


async def test_a_change_without_a_stage_is_quiet(monkeypatch):
    seen = []

    async def _sig(*a, **kw):
        seen.append(a)
        return None

    monkeypatch.setattr(V, "signal", _sig)
    await crm_engine.upsert_crm(_Pool("lead"), 42, "c-1",
                                {"next_reminder_text": "позвонить"})
    assert seen == []


async def test_the_manual_door_never_breaks_the_save(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("слой недоступен")

    monkeypatch.setattr(V, "signal", _boom)
    pool = _Pool("lead")
    await crm_engine.upsert_crm(pool, 42, "c-1", {"stage": "won"})
    assert any("UPDATE contact_crm" in q for q in pool.executed), (
        "сохранение стадии упало из-за слоя — слой вспомогательный")


def test_both_doors_use_the_same_mapping():
    """Два списка стадий уже разъехались один раз; второго раза не надо."""
    from services import intent_sensor
    assert "virtual_layer.STAGE_SIGNAL" in inspect.getsource(intent_sensor)
    assert "virtual_layer.STAGE_SIGNAL" in inspect.getsource(
        crm_engine._signal_virtual_layer)
