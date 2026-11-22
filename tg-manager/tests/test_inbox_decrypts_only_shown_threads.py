"""«Входящие»: расшифровывается текст только тех разговоров, что уйдут на экран.

`unified_inbox.list_threads` читает по строке на каждый разговор владельца
(десятки тысяч) и раньше расшифровывал последний текст КАЖДОГО, хотя на экран
уходит сотня. На стенде с 30 000 разговоров Хранилища экран открывался ~5,2 с,
после правки ~0,45 с. Проверяем, что расшифровок не больше лимита и что
показанный текст тот же, что раньше (включая случай, когда последнее слово —
за ботом, у которого текст открытый).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from services import unified_inbox as ui


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


class _Pool:
    def __init__(self, n):
        self.vault = [{"peer_user_id": i, "peer_name": f"П{i}", "peer_username": None,
                       "last_at": T0 + timedelta(minutes=i), "text_enc": f"E:текст {i}",
                       "last_direction": "out", "total": 1} for i in range(1, n + 1)]
        # Человек 1 потом написал боту — у него последнее слово открытым текстом.
        self.bots = [{"peer_user_id": 1, "bot_id": 7, "bot_username": "shop",
                      "last_at": T0 + timedelta(days=30), "history": '[{"role": "user", "content": "бот"}]',
                      "handed_off": False, "total": 1, "peer_name": None, "peer_username": None}]

    async def fetch(self, q, *a):
        if "FROM vault_messages" in q:
            return [dict(r) for r in self.vault]
        if "FROM bot_sales_dialogs" in q:
            return [dict(r) for r in self.bots]
        return []


def test_only_shown_threads_are_decrypted(monkeypatch):
    calls = []

    def _dec(enc):
        calls.append(enc)
        return enc[2:] if enc else ""

    monkeypatch.setattr(ui, "_decrypt", _dec)
    res = _run(ui.list_threads(_Pool(500), 1, limit=20))
    assert res["total"] == 500
    assert len(res["threads"]) == 20
    assert len(calls) <= 20, f"расшифровано {len(calls)} при 20 на экране"
    by = {t["peer_user_id"]: t for t in res["threads"]}
    assert by[1]["last_text"] == "бот", "последнее слово за ботом — его текст"
    assert by[500]["last_text"] == "текст 500"
    assert all("last_text_enc" not in t for t in res["threads"]), "шифр не уходит на экран"
