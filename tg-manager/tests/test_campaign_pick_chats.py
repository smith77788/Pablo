"""«💬 В чаты» → «Выбрать группы»: рассылка уходит только в отмеченные группы.

Раньше в чаты можно было написать только целыми аккаунтами — во все их группы
сразу. Теперь владелец отмечает отдельные группы, кампания ставит по операции
на аккаунт с его `chat_ids`, а group_announce шлёт ровно в них и не ходит за
живым списком диалогов (он режется на первых 50 и выронил бы часть выбора).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from services import account_manager, op_worker
from services import campaign_hub as ch

ROOT = Path(__file__).resolve().parent.parent


# ── Планировщик кампании ────────────────────────────────────────────────────

def _spec(chats: dict) -> dict:
    return {"content": {"text": "Привет"}, "targets": {"chats": {"enabled": True, **chats}}}


def test_picked_chats_fan_out_per_account():
    ops = ch.plan_campaign(_spec({"chat_ids": {"1": [10, 11, 10], "2": [20]}}))
    got = {op["params"]["acc_id"]: op["params"]["chat_ids"] for op in ops}
    assert got == {1: [10, 11], 2: [20]}
    assert {op["op_type"] for op in ops} == {"group_announce"}
    assert sorted(op["total_items"] for op in ops) == [1, 2]


@pytest.mark.parametrize("bad", [{}, {"1": []}, [], None, {"x": [1]}, {"0": [5]}])
def test_an_empty_or_broken_chat_pick_is_not_all_groups(bad):
    with pytest.raises(ch.CampaignSpecError, match="чат"):
        ch.plan_campaign(_spec({"acc_ids": [1, 2], "chat_ids": bad}))


def test_without_pick_whole_accounts_still_work():
    ops = ch.plan_campaign(_spec({"acc_ids": [1, 2]}))
    assert [op["params"]["acc_id"] for op in ops] == [1, 2]
    assert all("chat_ids" not in op["params"] for op in ops)


# ── Исполнитель group_announce ─────────────────────────────────────────────

class _Pool:
    def __init__(self, groups):
        self.groups = groups
        self.mc_args = None

    async def fetch(self, query, *args):
        if "FROM managed_channels" in query:
            self.mc_args = args
            return [g for g in self.groups if g["id"] in set(args[2])]
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        if "FROM tg_accounts WHERE id=" in query:
            return {"id": 7, "session_str": "s", "first_name": "a", "phone": "+1",
                    "device_model": "", "system_version": "", "app_version": "",
                    "lang_code": "", "system_lang_code": "", "proxy_url": None}
        return None

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.fixture
def posted(monkeypatch):
    out: list = []

    async def _post(session, target, text, **kw):
        out.append((target, kw.get("access_hash")))
        return {"msg_id": 1}

    async def _dialogs(*a, **k):
        raise AssertionError("при выбранных группах живой список диалогов не нужен")

    async def _true(*a, **k):
        return True

    async def _false(*a, **k):
        return False

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(account_manager, "get_dialogs", _dialogs)
    monkeypatch.setattr(op_worker, "try_claim_account", _true)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _false)
    real_sleep = asyncio.sleep

    async def _no_sleep(*_a, **_k):
        await real_sleep(0)

    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    op_worker._cancel_cache.clear()
    return out


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_announce_goes_only_to_picked_groups(posted):
    pool = _Pool([{"id": 10, "title": "A", "access_hash": 111},
                  {"id": 11, "title": "B", "access_hash": 222}])
    res = _run(op_worker._exec_group_announce(
        pool, None, 1, 777, {"acc_id": 7, "text": "x", "chat_ids": [11, 99]}))
    assert posted == [(11, 222)]
    assert pool.mc_args[:2] == (777, 7), "группы берутся только этого владельца и аккаунта"
    assert res["ok"] == 1 and "не найдено у аккаунта: 1" in res["summary"]


# ── Экран и маршрут ────────────────────────────────────────────────────────

def test_uni_campaign_screen_can_pick_groups():
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'id="ucChatScope"' in html and '<option value="pick">Выбрать группы</option>' in html
    assert "api(`/api/miniapp/campaign/chats?q=" in html
    spec = html[html.index("function _ucSpec()"):html.index("function _ucSummary(")]
    assert "chat_ids: picked" in spec


def test_chats_route_is_registered_before_campaign_id():
    src = (ROOT / "services" / "mini_app_broadcast.py").read_text(encoding="utf-8")
    a = src.index('add_get("/api/miniapp/campaign/chats"')
    b = src.index('add_get("/api/miniapp/campaign/{campaign_id}"')
    assert a < b, "/campaign/{campaign_id} перехватит /campaign/chats"
    assert re.search(r"mc\.owner_id=\$1", src)
