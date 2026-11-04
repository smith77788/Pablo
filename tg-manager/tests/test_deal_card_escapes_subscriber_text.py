"""Карточка сделки в боте: название, контакт и заметки — экранированный текст.

Сделки создаются автоматически из диалогов и сценариев: в название и контакт
попадает имя подписчика, в заметки — поля, которые он прислал. Карточка
выводилась с parse_mode=HTML как есть: «<» ломал экран (Telegram отклонял
разметку), а «<a href=…>» становилась ссылкой. Тест падает без фикса.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from bot.handlers import crm

EVIL = '<a href="https://phish.example">жми</a> & <'


async def test_deal_card_is_escaped(monkeypatch):
    deal = {"id": 1, "title": "Заявка — " + EVIL, "contact": EVIL, "stage": "lead",
            "value": None, "notes": EVIL}

    async def _deal(pool, deal_id, uid):
        return deal

    async def _acts(pool, deal_id, limit=5):
        return [{"created_at": datetime(2026, 1, 1), "note": EVIL}]

    monkeypatch.setattr(crm.db, "get_crm_deal", _deal)
    monkeypatch.setattr(crm.db, "get_crm_activity", _acts)
    out: list[str] = []

    async def _edit(text, **k):
        out.append(text)

    async def _ans(*a, **k):
        return None

    cb = SimpleNamespace(from_user=SimpleNamespace(id=5), answer=_ans,
                         message=SimpleNamespace(edit_text=_edit))
    await crm.cb_deal_view(cb, crm.CrmCb(action="deal_view", deal_id=1), None)
    assert out and "<a href" not in out[0] and "&lt;a href=" in out[0]


async def test_pipeline_list_is_escaped(monkeypatch):
    async def _deals(pool, owner_id, *a, **k):
        return [{"id": 1, "title": "<b>Иван</b> & <", "stage": "lead"}]

    monkeypatch.setattr(crm.db, "get_crm_deals", _deals)
    out: list[str] = []

    async def _edit(text, **k):
        out.append(text)

    async def _ans(*a, **k):
        return None

    cb = SimpleNamespace(from_user=SimpleNamespace(id=5), answer=_ans,
                         message=SimpleNamespace(edit_text=_edit))
    await crm.cb_crm_pipeline(cb, None)
    assert out and "<b>Иван</b>" not in out[0] and "&lt;b&gt;Иван" in out[0]
