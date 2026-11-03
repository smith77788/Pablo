"""Пауза перед авто-ответом не останавливает разбор и опрос.

ЧТО БЫЛО. У правила авто-ответа есть «пауза перед ответом» до 300 с
(человекоподобность). Ждалась она прямо в `_process_bot`. А полный проход
опроса ждёт ВСЕХ ботов разом (`asyncio.gather` в `run`), и вебхук держит замок
бота вместе с ответом Telegram. Значит одно правило с паузой 5 минут у одного
бота останавливало авто-ответы всех ботов всех клиентов на 5 минут — на
каждое совпавшее сообщение подряд, — а Telegram, не дождавшись ответа
вебхука, присылал то же обновление ещё раз.

ЧТО СТАЛО. Отложенный ответ уходит отдельной задачей; разбор пакета и сдвиг
оффсета идут сразу. Ответ по-прежнему отправляется — после паузы.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

from services import auto_responder as ar

BOT_ID = 556
TOKEN = "123456789:AAHrealtokenlooking-value_0123456789ab"


def _upd(update_id: int, text: str, user_id: int = 7002) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Артур"},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


class _FakePool:
    async def fetchrow(self, query, *args):
        if "FROM managed_bots" in query:
            return {"bot_role": None, "swarm_enabled": False, "cluster": None,
                    "added_by": 1, "username": "b", "first_name": "b",
                    "relay_enabled": False}
        return None

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "INSERT 0 1"


def _rule(delay: int) -> dict:
    return {"id": 1, "trigger_type": "keyword", "keyword": "цена",
            "match_mode": "contains", "response_text": "Цена — 100 ₽",
            "reply_delay_sec": delay, "active_from_hour": None,
            "active_to_hour": None, "buttons": None}


async def _drive(updates, rule, sleep_scale: float):
    sent: list[tuple[int, str]] = []
    saved: list[int] = []

    async def _set_offset(pool, bot_id, value):
        saved.append(value)

    async def _call(session, token, method, **params):
        if method == "getUpdates":
            return {"ok": True, "result": updates}
        return {"ok": True, "result": {}}

    async def _send(http, token, chat_id, text, buttons=None, **k):
        sent.append((chat_id, text))
        return True, None

    real_sleep = asyncio.sleep

    async def _scaled_sleep(sec, *a, **k):
        return await real_sleep(sec * sleep_scale)

    patches = [
        patch.object(ar.bot_api, "_call", _call),
        patch.object(ar.bot_api, "send_message", _send),
        patch("database.db.get_update_offset", AsyncMock(return_value=100)),
        patch("database.db.set_update_offset", _set_offset),
        patch("database.db.get_active_auto_replies", AsyncMock(return_value=[rule])),
        patch("database.db.get_active_funnels", AsyncMock(return_value=[])),
        patch("database.db.get_active_automation_rules", AsyncMock(return_value=[])),
        patch("database.db.get_active_experiment", AsyncMock(return_value=None)),
        patch("database.db.upsert_users", AsyncMock(return_value=[])),
        patch("database.db.record_message_keywords", AsyncMock()),
        patch.object(ar, "_record_bot_ok", AsyncMock()),
        patch.object(ar, "_record_bot_error", AsyncMock()),
        patch("services.brand_injection.is_free_tier", AsyncMock(return_value=False)),
        patch.object(ar.asyncio, "sleep", _scaled_sleep),
    ]
    for p in patches:
        p.start()
    try:
        t0 = time.monotonic()
        await ar._process_bot(_FakePool(), object(), BOT_ID, TOKEN)
        took = time.monotonic() - t0
        sent_before = list(sent)
        await asyncio.wait_for(
            asyncio.gather(*list(getattr(ar, "_delayed_replies", ()))), timeout=5)
    finally:
        for p in reversed(patches):
            p.stop()
    return took, sent_before, sent, saved


async def test_delayed_reply_does_not_hold_the_batch():
    # 300 с паузы, масштаб 1/100 → 3 с на ответ. Три совпавших сообщения:
    # раньше разбор пакета шёл бы 9 с (масштабно — 15 минут).
    updates = [_upd(101, "цена?", 1), _upd(102, "а цена", 2), _upd(103, "цена!", 3)]
    took, sent_before, sent_after, saved = await _drive(
        updates, _rule(300), sleep_scale=0.01)
    assert took < 1.5, f"разбор пакета ждал паузу авто-ответа: {took:.1f} с"
    assert saved and saved[-1] == 103, "оффсет сдвинут сразу, не после пауз"
    replies = [c for c, t in sent_after if t.startswith("Цена")]
    assert sorted(replies) == [1, 2, 3], "отложенные ответы всё равно ушли"
    assert not [c for c, t in sent_before if t.startswith("Цена")], \
        "ответ ушёл раньше паузы"
    assert not getattr(ar, "_delayed_replies", ()), "задачи держатся только до конца"


async def test_reply_without_delay_is_sent_inline():
    took, sent_before, _after, _saved = await _drive(
        [_upd(101, "цена?", 1)], _rule(0), sleep_scale=1.0)
    assert [c for c, t in sent_before if t.startswith("Цена")] == [1]
