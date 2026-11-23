"""Настройки каналов/групп и каналы-приёмники уведомлений о ботах.

1. Настроек каналов в продукте не было вовсе: только название, описание,
   username и аватар. Теперь — тип, обсуждение, подпись, запрет копирования,
   реакции, автоудаление, заявки; у групп — разрешения, медленный режим, темы,
   история для новичков (services/channel_settings.py).
2. Уведомления о запусках ботов и заявках можно направить в свой канал на
   каждую сеть проектов (services/bot_notify_routes.py).
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from services import bot_event_notify as ben
from services import bot_notify_routes as R
from services import channel_settings as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Настройки канала: проверка значений ───────────────────────────────────

def test_validate_scopes_channel_vs_group():
    assert cs.validate("signatures", True, is_group=False) == (True, "")
    assert cs.validate("signatures", True, is_group=True)[1]
    assert cs.validate("slowmode", 30, is_group=True) == (30, "")
    assert cs.validate("slowmode", 30, is_group=False)[1]
    assert cs.validate("nope", 1, is_group=False)[1]


def test_validate_values():
    assert cs.validate("username", "https://t.me/My_chan1", is_group=False) == ("My_chan1", "")
    assert cs.validate("username", "", is_group=False) == ("", "")          # частный
    assert cs.validate("username", "1abc", is_group=False)[1]
    assert cs.validate("ttl", "86400", is_group=False) == (86400, "")
    assert cs.validate("ttl", 5, is_group=False)[1]
    assert cs.validate("slowmode", 45, is_group=True)[1]
    assert cs.validate("reactions", "ALL", is_group=False) == ("all", "")
    assert cs.validate("noforwards", "yes", is_group=False)[1]
    assert cs.validate("permissions", {"send_polls": False}, is_group=True) == (
        {"send_polls": False}, "")
    assert cs.validate("permissions", {"root": True}, is_group=True)[1]


def test_permission_change_keeps_the_other_rights():
    """Запрос заменяет права целиком: правка одного тумблера не снимает остальные."""
    current = {k: True for k in cs.PERM_RIGHTS}
    current["embed_links"] = False
    kw = cs.rights_kwargs(current, {"send_polls": False})
    assert kw["send_polls"] is True          # запрещено (флаг = запрет)
    assert kw["embed_links"] is True         # прежний запрет сохранён
    assert kw["send_plain"] is False and kw["send_messages"] is False
    assert all(kw[f] is False for f in cs.PERM_RIGHTS["send_media"])


def test_snapshot_of_group_and_channel():
    rights = SimpleNamespace(send_polls=True, send_photos=True)
    group = SimpleNamespace(megagroup=True, title="Чат", username="", noforwards=False,
                            join_request=True, forum=True, join_to_send=False,
                            default_banned_rights=rights, creator=True)
    full = SimpleNamespace(about="", ttl_period=86400, participants_count=14,
                           admins_count=2, kicked_count=0, requests_pending=3,
                           linked_chat_id=None, slowmode_seconds=30,
                           hidden_prehistory=True, available_reactions=None)
    s = cs.snapshot(full, group)
    assert s["is_group"] and s["slowmode"] == 30 and s["forum"] and s["ttl"] == 86400
    assert s["permissions"]["send_polls"] is False
    assert s["permissions"]["send_media"] is False     # фото запрещены → медиа нет
    assert s["permissions"]["send_plain"] is True
    assert s["requests_pending"] == 3
    chan = SimpleNamespace(megagroup=False, title="Канал", username="infra",
                           signatures=True, noforwards=True, join_request=False)
    sc = cs.snapshot(SimpleNamespace(participants_count=26, admins_count=3), chan)
    assert not sc["is_group"] and sc["signatures"] and sc["noforwards"]
    assert "slowmode" not in sc and sc["username"] == "infra"


def test_settings_reachable_from_channel_card():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "openChSettings(" in html and 'src="screens/channel_settings.js"' in html
    js = open(os.path.join(ROOT, "mini_app", "screens", "channel_settings.js"),
              encoding="utf-8").read()
    assert "/settings" in js and "function openChSettings" in js


# ── Каналы уведомлений ────────────────────────────────────────────────────

def test_route_matches_bots_and_events():
    r = {"enabled": True, "events": ["start"], "bot_ids": [5, 6]}
    assert R.matches(r, 5, "start")
    assert not R.matches(r, 7, "start")
    assert not R.matches(r, 5, "join_request")
    assert R.matches({"enabled": True, "events": ["join_request"], "bot_ids": None}, 99, "join_request")
    assert not R.matches({"enabled": False, "bot_ids": None}, 1, "start")
    assert R.matches({"enabled": True, "events": '["start"]', "bot_ids": "[0]"}, 0, "start")


def test_route_normalize():
    ok, err = R.normalize({"chat_id": "-1001", "events": ["start", "x"], "bot_ids": [3, "3", 4]})
    assert not err and ok["events"] == ["start"] and ok["bot_ids"] == [3, 4]
    assert R.normalize({"events": ["start"]})[1]
    assert R.normalize({"chat_id": 1, "events": []})[1]
    assert R.normalize({"chat_id": 1, "events": ["start"], "bot_ids": []})[1]
    assert R.normalize({"chat_id": 1, "events": ["start"]})[0]["bot_ids"] is None


class _Pool:
    def __init__(self, routes):
        self.routes = routes
        self.marks = []

    async def fetch(self, q, *a):
        return self.routes

    async def execute(self, q, *a):
        self.marks.append(a)
        return "UPDATE 1"


class _Bot:
    def __init__(self, fail=()):
        self.sent = []
        self.fail = set(fail)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail:
            raise RuntimeError("Forbidden: bot was kicked from the channel chat")
        self.sent.append((chat_id, text))


def _route(i, chat, bots=None, events=("start", "join_request"), mute=False):
    return {"id": i, "name": f"сеть {i}", "chat_id": chat, "chat_title": "", "bot_ids": bots,
            "events": list(events), "mute_dm": mute, "enabled": True, "last_error": None}


def test_event_goes_to_every_matching_channel():
    pool = _Pool([_route(1, -101, [5]), _route(2, -102, None), _route(3, -103, [6])])
    bot = _Bot()
    mute = asyncio.run(R.deliver(pool, bot, 1, bot_id=5, event="start", text="t"))
    assert [c for c, _ in bot.sent] == [-101, -102]
    assert mute is False


def test_dm_is_muted_only_after_real_delivery():
    pool = _Pool([_route(1, -101, None, mute=True)])
    assert asyncio.run(R.deliver(pool, _Bot(), 1, bot_id=5, event="start", text="t")) is True
    pool = _Pool([_route(1, -101, None, mute=True)])
    assert asyncio.run(R.deliver(pool, _Bot(fail={-101}), 1, bot_id=5,
                                 event="start", text="t")) is False
    assert pool.marks and "убрали" in pool.marks[-1][1]


def test_notify_start_uses_channel_then_dm(monkeypatch):
    ben._buckets.clear()
    dm = []

    async def _dm(pool, bot, owner, pref, text, **kw):
        dm.append(text)
        return True

    monkeypatch.setattr("database.db.notify_if_enabled", _dm)

    async def go(routes):
        bot = _Bot()
        await ben.notify_start(_Pool(routes), bot, 1, bot_id=5, bot_name="b",
                               event={"kind": "new", "who": "@x"})
        await asyncio.sleep(0.05)
        return bot

    bot = asyncio.run(go([_route(1, -101, [5], mute=True)]))
    assert bot.sent and not dm                       # только канал
    ben._buckets.clear()
    bot = asyncio.run(go([_route(1, -101, [5], mute=False)]))
    assert bot.sent and dm                           # канал и личка


def test_notify_routes_ui_reachable():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "openNotifyRoutes()" in html and 'src="screens/notify_routes.js"' in html
