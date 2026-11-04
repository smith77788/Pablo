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
    done, left = asyncio.run(crm.deliver_group(None, None, items))
    assert left == []
    got = {it["event"]: res[0] for it, res in done}
    assert got == {"contact.started": True, "deal.updated": False, "order.created": True}
