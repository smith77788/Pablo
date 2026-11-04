"""Сбой одной доставки в CRM не роняет пачку подключения.

ЧТО БЫЛО. `crm_connectors.deliver_group` шёл по событиям подключения подряд,
и исключение одного (сбой базы, неожиданный ответ вне разбора) улетало
наружу. Итог уже доставленных в этой пачке строк не записывался: после аренды
они уходили в CRM второй раз — вторые заметки, вторые сделки.
"""
from __future__ import annotations

import asyncio


def test_one_crashing_event_does_not_lose_the_others(monkeypatch):
    from services import crm_connectors as crm

    async def _deliver(pool, http, ep, event, data):
        if event == "deal.updated":
            raise RuntimeError("база недоступна")
        return True, 200, ""

    monkeypatch.setattr(crm, "deliver", _deliver)
    items = [{"id": i, "event_id": i, "endpoint_id": 1, "endpoint": {},
              "event": ev, "payload": {}}
             for i, ev in enumerate(("contact.started", "deal.updated", "order.created"), 1)]
    done, left, pause = asyncio.run(crm.deliver_group(None, None, items))
    assert left == [] and pause == 0
    got = {it["event"]: res[0] for it, res in done}
    assert got == {"contact.started": True, "deal.updated": False, "order.created": True}


def test_crm_rate_limit_pauses_the_group_without_spending_attempts(monkeypatch):
    """CRM попросила реже: раньше каждое следующее событие пачки било в тот же
    лимит, тратило попытку и удлиняло серию неудач. Теперь пачка встаёт, и
    событие с остатком возвращаются в очередь после паузы."""
    from services import crm_connectors as crm

    calls = []

    async def _deliver(pool, http, ep, event, data):
        calls.append(event)
        if len(calls) == 2:
            return False, crm.RATE_LIMITED, "amoCRM просит реже"
        return True, 200, ""

    monkeypatch.setattr(crm, "deliver", _deliver)
    items = [{"id": i, "event_id": i, "endpoint_id": 1, "endpoint": {},
              "event": f"e{i}", "payload": {}} for i in range(1, 5)]
    done, left, pause = asyncio.run(crm.deliver_group(None, None, items))
    assert calls == ["e1", "e2"], "после лимита CRM пачка продолжала бить в него"
    assert [it["event"] for it, _ in done] == ["e1"]
    assert [it["event"] for it in left] == ["e2", "e3", "e4"]
    assert pause == crm.RATE_LIMIT_PAUSE_S


def test_rate_limit_error_is_reported_as_rate_limited(monkeypatch):
    from services import crm_connectors as crm

    async def _boom(*a, **k):
        raise crm.CrmError("Bitrix24 просит реже", 200, rate_limited=True)

    monkeypatch.setattr(crm, "HANDLERS", {"deal.updated": _boom})
    monkeypatch.setattr(crm, "_client_for", lambda http, ep: None)
    ok, code, _msg = asyncio.run(crm.deliver(None, None, {"owner_id": 1, "id": 1},
                                             "deal.updated", {}))
    assert (ok, code) == (False, crm.RATE_LIMITED)
