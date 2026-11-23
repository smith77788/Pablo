"""Уведомления «запустили бота» и «заявка на вступление» не теряются.

ЧТО БЫЛО (жалоба владельца: «не всегда приходят уведомления»):
1. Троттлинг на бота копил подписчиков внутри минутного окна, но отдавал
   накопленное только со СЛЕДУЮЩИМ подписчиком после окна. Двое за минуту и
   дальше тишина — про второго владелец не узнавал.
2. Поверх него `notify_if_enabled` держал свой минутный кулдаун на том же
   ключе и молча глотал уведомление, разрешённое первым окном.
3. Повторный /start человека, писавшего боту раньше, не уведомлял вовсе.
4. `chat_join_request` не запрашивался ни главным ботом, ни подключёнными,
   и обработчика не было — о заявках в канал владелец не узнавал никогда.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from services import auto_responder as ar
from services import bot_event_notify as ben

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reset():
    ben._buckets.clear()


# ── Окно: первое сразу, остальное — сводкой по таймеру ────────────────────

def test_first_event_goes_out_immediately():
    _reset()
    assert ben.take("k1", {"who": "a"}, now=1000.0) == [{"who": "a"}]


def test_events_inside_window_are_held_not_dropped():
    _reset()
    ben.take("k2", {"who": "a"}, now=1000.0)
    assert ben.take("k2", {"who": "b"}, now=1010.0) is None
    assert ben.pending("k2") == 1
    assert ben.due_in("k2", now=1010.0) == ben.WINDOW_S - 10
    assert ben.flush("k2", now=1060.0) == [{"who": "b"}]
    assert ben.pending("k2") == 0


def test_held_event_is_delivered_by_timer_without_a_next_event(monkeypatch):
    """Регресс причины 1: второй за окно доходит, даже если третьего нет."""
    _reset()
    monkeypatch.setattr(ben, "WINDOW_S", 0.05)
    sent: list[list[dict]] = []

    async def send(batch):
        sent.append(batch)

    async def scenario():
        ben.submit("k3", {"who": "a"}, send)
        ben.submit("k3", {"who": "b"}, send)
        ben.submit("k3", {"who": "c"}, send)
        await asyncio.sleep(0.2)   # больше никто не пришёл

    asyncio.run(scenario())
    assert sent == [[{"who": "a"}], [{"who": "b"}, {"who": "c"}]], sent


def test_only_one_timer_per_source(monkeypatch):
    _reset()
    monkeypatch.setattr(ben, "WINDOW_S", 0.05)
    sent: list[list[dict]] = []

    async def send(batch):
        sent.append(batch)

    async def scenario():
        for i in range(50):
            ben.submit("k4", {"i": i}, send)
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    # Анти-накрутка сохранена: 50 событий — два сообщения, а не пятьдесят.
    assert len(sent) == 2
    assert sum(len(b) for b in sent) == 50


def test_sources_do_not_share_a_window():
    _reset()
    assert ben.take("start:1", {"x": 1}, now=5.0)
    assert ben.take("start:2", {"x": 2}, now=5.0)


def test_dedup_keys_are_unique():
    """Регресс причины 2: у notify_if_enabled нет общего слота с соседом."""
    assert ben.dedup_key("start:1") != ben.dedup_key("start:1")


# ── Тексты ────────────────────────────────────────────────────────────────

def test_start_note_escapes_telegram_names():
    ev = ben.start_event({"first_name": "<b>Вася</b>"}, 5, new=True)
    note = ben.start_note("mybot", [ev])
    assert "&lt;b&gt;Вася" in note and "@mybot" in note


def test_start_note_aggregate_counts_new_and_returning():
    batch = [ben.start_event({"username": f"u{i}"}, i, new=i % 2 == 0) for i in range(7)]
    note = ben.start_note("mybot", batch)
    assert "7 раз" in note and "4 новых" in note and "3 вернулись" in note
    assert "и ещё 2" in note


def test_join_note_single_and_many():
    cjr = {"chat": {"id": -1001, "title": "Канал"},
           "from": {"id": 9, "username": "petya"}, "bio": "люблю <код>"}
    one = ben.join_note(cjr["chat"], [ben.join_event(cjr)], via_bot="helper")
    assert "Заявка на вступление" in one and "@petya" in one
    assert "&lt;код&gt;" in one and "@helper" in one
    many = ben.join_note(cjr["chat"], [ben.join_event(cjr)] * 3)
    assert "3 заявки" in many


def test_channel_id_variants_cover_bot_api_and_mtproto():
    assert ben.channel_id_variants(-1001234567890) == [-1001234567890, 1234567890]
    assert ben.channel_id_variants(1234567890) == [1234567890, -1001234567890]


# ── Повторный /start ──────────────────────────────────────────────────────

def test_returning_after_silence():
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert ar._returned_after_silence(now - timedelta(days=2), now)
    assert not ar._returned_after_silence(now - timedelta(minutes=5), now)
    assert not ar._returned_after_silence(None, now)


# ── Заявки запрашиваются и разбираются ────────────────────────────────────

def test_join_requests_are_requested_from_telegram():
    """Регресс причины 4: без типа в allowed_updates Telegram заявку не пришлёт."""
    assert "chat_join_request" in ar._MANAGED_BOT_ALLOWED_UPDATES
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    start = src.index("_allowed_updates = [")
    assert '"chat_join_request"' in src[start:src.index("]", start)]
    assert "join_requests_handler.router" in src


def test_old_webhooks_are_reregistered_for_new_types():
    from services import managed_bot_webhooks as mbw

    assert not mbw.allowed_updates_current({"allowed_updates": ["message"]})
    assert not mbw.allowed_updates_current({})
    assert mbw.allowed_updates_current(
        {"allowed_updates": list(ar._MANAGED_BOT_ALLOWED_UPDATES)})


# ── Разбор апдейта подключённого бота ─────────────────────────────────────

class _Pool:
    async def fetchrow(self, query, *args):
        if "FROM managed_bots" in query:
            return {"bot_role": None, "swarm_enabled": False, "cluster": None,
                    "added_by": 77, "username": "helper", "first_name": "h",
                    "relay_enabled": False}
        return None

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "UPDATE 1"


def _drive(updates, record=(False, None)):
    calls = {"join": [], "start": []}

    async def _join(pool, main_bot, owner_id, **kw):
        calls["join"].append((owner_id, kw))

    async def _start(pool, main_bot, owner_id, **kw):
        calls["start"].append((owner_id, kw))

    async def _call(session, token, method, **params):
        return {"ok": True, "result": updates if method == "getUpdates" else {}}

    patches = [
        patch.object(ar.bot_api, "_call", _call),
        patch.object(ar._ben, "notify_join_request", _join),
        patch.object(ar._ben, "notify_start", _start),
        patch("database.db.get_update_offset", AsyncMock(return_value=10)),
        patch("database.db.set_update_offset", AsyncMock()),
        patch("database.db.get_active_auto_replies", AsyncMock(return_value=[])),
        patch("database.db.get_active_funnels", AsyncMock(return_value=[])),
        patch("database.db.get_active_automation_rules", AsyncMock(return_value=[])),
        patch("database.db.get_active_experiment", AsyncMock(return_value=None)),
        patch("database.db.record_user_activity", AsyncMock(return_value=record)),
        patch("database.db.upsert_users", AsyncMock(return_value=[])),
        patch.object(ar, "_record_bot_ok", AsyncMock()),
        patch.object(ar, "_record_bot_error", AsyncMock()),
        patch("services.brand_injection.is_free_tier", AsyncMock(return_value=False)),
    ]
    for p in patches:
        p.start()
    try:
        asyncio.run(ar._process_bot(_Pool(), object(), 5, "1:tok", main_bot=object()))
    finally:
        for p in reversed(patches):
            p.stop()
    return calls


def _start_upd(uid=901):
    return {"update_id": 11, "message": {
        "message_id": 1, "from": {"id": uid, "is_bot": False, "first_name": "А"},
        "chat": {"id": uid, "type": "private"}, "text": "/start"}}


def test_join_request_to_managed_bot_reaches_owner():
    upd = {"update_id": 11, "chat_join_request": {
        "chat": {"id": -1009, "type": "channel", "title": "Канал"},
        "from": {"id": 42, "first_name": "Петя"}, "user_chat_id": 42, "date": 1}}
    calls = _drive([upd])
    assert len(calls["join"]) == 1
    owner, kw = calls["join"][0]
    assert owner == 77 and kw["bot_id"] == 5 and kw["via_bot"] == "helper"


def test_new_user_start_notifies():
    calls = _drive([_start_upd()], record=(True, None))
    assert len(calls["start"]) == 1 and calls["start"][0][0] == 77


def test_returning_user_start_notifies():
    """Регресс причины 3: /start знакомого, но давно молчавшего человека."""
    old = datetime.now(timezone.utc) - timedelta(days=3)
    calls = _drive([_start_upd()], record=(False, old))
    assert len(calls["start"]) == 1
    assert calls["start"][0][1]["event"]["kind"] == "back"


def test_active_user_start_is_quiet():
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    assert _drive([_start_upd()], record=(False, recent))["start"] == []
