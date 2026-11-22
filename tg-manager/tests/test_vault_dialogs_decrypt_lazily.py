"""Список диалогов Хранилища расшифровывает только то, что нужно.

Раньше `vault_dialogs.dialogs` расшифровывал ДВА текста на каждый диалог
владельца (превью и последнее живое входящее), хотя на экран уходит страница.
На стенде с 30 000 диалогов `/api/miniapp/vault/dialogs` шёл ~10 с, после
правки ~0,45 с; сводка организма, которая читает тот же список, — 5 с → 0,3 с.

Проверяем: расшифровок не больше, чем строк на странице плюс диалогов, где
решается «спасибо/вопрос»; превью и решение «ждёт ответа» те же.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from services import vault_dialogs as vd


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


T0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)


def _row(i, *, live_in=False, text="привет"):
    last = T0 + dt.timedelta(minutes=i)
    return {"chat_id": i, "peer_name": f"П{i}", "peer_username": None, "total": 1,
            "deleted": 0, "media": 0, "first_date": last, "last_date": last,
            "last_out": None if live_in else last, "last_in": last,
            "text_enc": f"E:{text} {i}", "media_type": None, "last_deleted": False,
            "last_direction": "in" if live_in else "out",
            "live_text_enc": f"E:{text} {i}", "live_media_type": None,
            "live_direction": "in" if live_in else "out",
            "pinned": False, "archived": False, "muted": False, "read_at": None,
            "handled_at": None, "purchased_at": None, "note": None, "unread": 0}


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        if "WITH agg AS" in q:
            return [dict(r) for r in self.rows]
        return []


def test_page_decrypts_only_what_it_shows(monkeypatch):
    calls = []

    def _dec(enc):
        calls.append(enc)
        return enc[2:]

    monkeypatch.setattr(vd, "decrypt_token", _dec)
    rows = [_row(i) for i in range(1, 1001)]
    # Двое ждут ответа: один спросил, другой сказал «спасибо».
    rows += [_row(2001, live_in=True, text="сколько стоит?"),
             _row(2002, live_in=True, text="Спасибо!")]
    res = _run(vd.dialogs(_Pool(rows), 1, limit=20))
    assert res["total"] == 1002
    assert len(res["chats"]) == 20
    assert len(calls) <= 20 + 2, f"расшифровано {len(calls)} при 20 строках на экране"
    assert res["counts"]["waiting"] == 1, "«спасибо» не ждёт ответа, вопрос — ждёт"
    top = res["chats"][0]
    assert top["chat_id"] == 2002 and top["last_preview"] == "Спасибо! 2002"
    assert all("_text_enc" not in c for c in res["chats"]), "шифр не уходит на экран"


def test_search_still_looks_into_preview(monkeypatch):
    monkeypatch.setattr(vd, "decrypt_token", lambda enc: enc[2:])
    rows = [_row(i) for i in range(1, 50)] + [_row(77, text="особый заказ")]
    res = _run(vd.dialogs(_Pool(rows), 1, q="особый"))
    assert [c["chat_id"] for c in res["chats"]] == [77]
