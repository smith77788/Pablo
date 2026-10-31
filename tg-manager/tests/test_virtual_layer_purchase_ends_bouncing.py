"""Купивший больше не «ходит по кругу».

Владелец получал «5 ходят по кругу» каждые несколько часов про людей, которые
уже заплатили: отметка «bouncing» жила отдельным ключом и не снималась ничем,
а покупка до модели вообще не доходила — ни стадия CRM «Выиграно», ни
выполненный заказ бота не превращались в сигнал «paid».
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

from services import virtual_layer as vl

T0 = datetime(2026, 10, 31, tzinfo=timezone.utc)


# ── Чистые правила ─────────────────────────────────────────────────────────

def test_payment_lifts_out_of_lost():
    """Оплата — факт: списанный в «потерян», но купивший, становится «купил»."""
    ch = vl.apply_signal({"value": vl.LOST, "confidence": 0.9}, "paid", now=T0)
    assert ch and ch["value"] == "purchased"


def test_other_signals_still_cannot_lift_lost():
    assert vl.apply_signal({"value": vl.LOST, "confidence": 0.9}, "replied",
                           now=T0) is None


def test_payment_from_any_rung_is_purchase():
    ch = vl.apply_signal({"value": "interested", "confidence": 0.6}, "paid", now=T0)
    assert ch["value"] == "purchased"


# ── Слой: снятие отметки ───────────────────────────────────────────────────

class _Pool:
    """Состояния в памяти: {(entity_id, state_key): value}."""

    def __init__(self, states=None):
        self.states = dict(states or {})
        self.history: list[tuple] = []

    async def fetchrow(self, sql, *args):
        _owner, _etype, eid, key = args[:4]
        v = self.states.get((str(eid), key))
        return {"value": v, "confidence": 0.7, "source": None,
                "expires_at": None, "updated_at": None} if v else None

    async def execute(self, sql, *args):
        if "INSERT INTO virtual_states" in sql:
            self.states[(str(args[2]), args[3])] = args[4]
        else:
            self.history.append(args)

    async def fetch(self, sql, *args):
        return []


async def test_purchase_signal_clears_bouncing(monkeypatch):
    from services.organism import spine

    async def _emit(*a, **k):
        return None
    monkeypatch.setattr(spine, "emit", _emit)
    pool = _Pool({("c-1", "funnel"): "interested", ("c-1", "pattern"): "bouncing"})
    await vl.signal(pool, 1, vl.USER, "c-1", "paid", confidence=0.95)
    assert pool.states[("c-1", "funnel")] == "purchased"
    assert pool.states[("c-1", "pattern")] != "bouncing"


async def test_mark_purchased_clears_even_already_purchased(monkeypatch):
    """Старые данные: «купил» уже стоит, а отметка круга так и висит."""
    from services.organism import spine

    async def _emit(*a, **k):
        return None
    monkeypatch.setattr(spine, "emit", _emit)
    pool = _Pool({("c-1", "funnel"): "purchased", ("c-1", "pattern"): "bouncing"})
    await vl.mark_purchased(pool, 1, vl.USER, "c-1")
    assert pool.states[("c-1", "pattern")] == vl.CONVERTED


async def test_detector_skips_buyers():
    class _P(_Pool):
        async def fetch(self, sql, *args):
            if "HAVING" in sql:
                return [{"entity_id": "c-1"}]
            return [
                {"entity_id": "c-1", "from_value": a, "to_value": b,
                 "created_at": None}
                for a, b in [(None, "curious"), ("curious", "interested"),
                             ("interested", "curious"), ("curious", "interested"),
                             ("interested", "purchased")]]
    pool = _P({("c-1", "funnel"): "purchased"})
    assert await vl.detect_bouncing(pool, 1) == []
    assert ("c-1", "pattern") not in pool.states


# ── Счётчик уведомления и проводка покупок ─────────────────────────────────

def test_nudge_count_excludes_buyers():
    from services.organism import world
    src = inspect.getsource(world)
    i = src.index("out[\"bouncing\"]")
    chunk = src[i:i + 700]
    assert "purchased" in chunk, "уведомление считает купивших «по кругу»"


def test_crm_won_reaches_the_layer():
    """«Выиграно» в CRM — это оплата, а оплата снимает отметку круга."""
    assert vl.STAGE_SIGNAL["won"] == "paid"
    src = inspect.getsource(vl.signal)
    assert "clear_bouncing" in src


def test_done_bot_order_reaches_the_layer():
    from services import bot_sales_persona
    src = inspect.getsource(bot_sales_persona.update_order)
    assert '"done"' in src and '"paid"' in src


def test_owner_can_mark_purchase_from_miniapp():
    from tests.miniapp_source import api_routes, miniapp_source
    assert ("/api/miniapp/vlayer/entity/{entity_type}/{entity_id}/purchased"
            in api_routes())
    t = miniapp_source()
    assert "vlMarkPurchased" in t and "Ходят по кругу" in t
