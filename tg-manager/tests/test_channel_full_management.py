"""Полное управление каналом: удалить в Telegram, очистить посты, выйти, автопостинг.

Раньше «🗑 Убрать» стирало только запись в Infragram: канал жил в Telegram со
всеми постами, а удалить его совсем, очистить посты или выйти из чужого канала
было нечем. Здесь проверяем всю цепочку: подтверждение словом на сервере,
исполнитель bulk_chan_exec, сами вызовы Telegram и следы в базе после удаления.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from aiohttp import ClientSession, web

from services import account_manager
from services import mini_app_api as M
from services import op_worker

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "mini_app" / "screens" / "channel_manage.js").read_text(encoding="utf-8")
UID = 880022


# ── Подтверждение и разбор параметров ─────────────────────────────────────

def test_irreversible_ops_need_typed_word():
    assert not M.channel_manage_confirmed("delete", {})
    assert not M.channel_manage_confirmed("delete", {"confirm": "да"})
    assert not M.channel_manage_confirmed("clear", {"confirm": True})
    assert M.channel_manage_confirmed("delete", {"confirm": " удалить "})
    assert M.channel_manage_confirmed("clear", {"confirm": "УДАЛИТЬ"})
    # Выход из канала обратим (можно вступить снова) — слово не нужно.
    assert M.channel_manage_confirmed("leave", {})


def test_clear_limit_never_turns_garbage_into_all():
    assert M.channel_clear_limit({}) == 0
    assert M.channel_clear_limit({"clear_limit": 0}) == 0
    assert M.channel_clear_limit({"clear_limit": "50"}) == 50
    # Опечатка не должна означать «стереть всё».
    assert M.channel_clear_limit({"clear_limit": "abc"}) == -1
    assert M.channel_clear_limit({"clear_limit": -5}) == -1


def test_manage_ops_kept_apart_from_reversible_edits():
    assert M.channel_manage_worker_op("delete") == "chan_delete"
    assert M.channel_manage_worker_op("clear") == "chan_clear"
    assert M.channel_manage_worker_op("leave") == "chan_leave"
    assert M.channel_manage_worker_op("title") is None
    assert M.channel_edit_worker_op("delete") is None


# ── Исполнитель bulk_chan_exec ────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "OK"

    async def fetch(self, q, *a):
        self.executed.append((q, a))
        if "FROM operation_queue" in q:
            return [{"id": 77}]
        return []
    async def fetchval(self, q, *a): return 0
    async def fetchrow(self, q, *a): return None


def _wire_worker(monkeypatch):
    async def _select_all_active(pool, owner_id, **kw):
        return [{"id": 5, "session_str": "s5", "first_name": "acc"}]
    async def _claim(ids): return list(ids)
    async def _release(ids): return None
    async def _cancelled(pool, op_id): return False
    async def _nosleep(*_a, **_k): return None
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)
    monkeypatch.setattr(op_worker.asyncio, "sleep", _nosleep)


_PAIR = {"channel_id": 111, "acc_id": 5, "title": "Новости",
         "access_hash": 999, "username": "news", "type": "channel"}


def test_delete_without_confirmed_flag_touches_nothing(monkeypatch):
    _wire_worker(monkeypatch)
    called = []

    async def _destroy(*a, **k):
        called.append(a)
        return {"ok": True}
    monkeypatch.setattr(account_manager, "destroy_channel", _destroy)
    pool = _Pool()
    res = asyncio.run(op_worker._exec_bulk_chan_exec(
        pool, None, 1, 42, {"op": "chan_delete", "channel_acc_pairs": [_PAIR]}))
    assert res["status"] == "failed"
    assert not called, "без подтверждения канал удалять нельзя"
    assert not any("DELETE FROM managed_channels" in q for q, _ in pool.executed)


def test_delete_destroys_in_telegram_and_forgets_channel(monkeypatch):
    _wire_worker(monkeypatch)
    got = {}

    async def _destroy(session, ch_id, _acc=None, access_hash=0, username="", chat_type=""):
        got.update(ch_id=ch_id, access_hash=access_hash, username=username, chat_type=chat_type)
        return {"ok": True}
    monkeypatch.setattr(account_manager, "destroy_channel", _destroy)
    cancelled = []

    async def _cancel(pool, op_id, owner_id, allow=()):
        cancelled.append((op_id, owner_id, tuple(allow)))
        return True
    monkeypatch.setattr(op_worker._obus_reg, "cancel", _cancel)
    pool = _Pool()
    res = asyncio.run(op_worker._exec_bulk_chan_exec(
        pool, None, 1, 42,
        {"op": "chan_delete", "confirmed": True, "channel_acc_pairs": [_PAIR]}))
    assert res["ok"] == 1 and res["fail"] == 0
    assert got == {"ch_id": 111, "access_hash": 999, "username": "news", "chat_type": "channel"}
    sql = " ".join(q for q, _ in pool.executed)
    assert "DELETE FROM managed_channels" in sql
    assert "DELETE FROM va_channel_admin" in sql
    # Автопостинг в удалённый канал гасится через общую дверь отмены
    # (с объявлением исхода), а идущую сейчас операцию не трогает.
    assert cancelled == [(77, 42, ("pending", "scheduled", "paused"))]
    assert "удалён в Telegram" in res["summary"]


def test_failed_delete_keeps_channel_and_says_why(monkeypatch):
    _wire_worker(monkeypatch)

    async def _destroy(*a, **k):
        return {"ok": False, "error": "у аккаунта нет прав: удалить канал может только его создатель"}
    monkeypatch.setattr(account_manager, "destroy_channel", _destroy)
    pool = _Pool()
    res = asyncio.run(op_worker._exec_bulk_chan_exec(
        pool, None, 1, 42,
        {"op": "chan_delete", "confirmed": True, "channel_acc_pairs": [_PAIR]}))
    assert res["ok"] == 0 and res["fail"] == 1
    assert "создатель" in res["summary"]
    assert not any("DELETE FROM managed_channels" in q for q, _ in pool.executed)


def test_clear_passes_limit_and_reports_count(monkeypatch):
    _wire_worker(monkeypatch)
    got = {}

    async def _clear(session, ch_id, _acc=None, access_hash=0, username="", limit=0):
        got.update(ch_id=ch_id, limit=limit, access_hash=access_hash)
        return {"ok": True, "deleted": 37}
    monkeypatch.setattr(account_manager, "clear_channel_posts", _clear)
    pool = _Pool()
    res = asyncio.run(op_worker._exec_bulk_chan_exec(
        pool, None, 1, 42,
        {"op": "chan_clear", "confirmed": True, "clear_limit": 50,
         "channel_acc_pairs": [_PAIR]}))
    assert got == {"ch_id": 111, "limit": 50, "access_hash": 999}
    assert res["ok"] == 1 and "37" in res["summary"]
    # Очистка постов сам канал из Infragram не убирает.
    assert not any("DELETE FROM managed_channels" in q for q, _ in pool.executed)


def test_leave_forgets_channel(monkeypatch):
    _wire_worker(monkeypatch)

    async def _leave(*a, **k):
        return {"ok": True}
    monkeypatch.setattr(account_manager, "leave_managed_channel", _leave)
    pool = _Pool()
    res = asyncio.run(op_worker._exec_bulk_chan_exec(
        pool, None, 1, 42, {"op": "chan_leave", "channel_acc_pairs": [_PAIR]}))
    assert res["ok"] == 1
    assert any("DELETE FROM managed_channels" in q for q, _ in pool.executed)


# ── Вызовы Telegram ───────────────────────────────────────────────────────

class _Msg:
    def __init__(self, i): self.id = i


class _Affected:
    def __init__(self, n): self.pts_count = n


class _Iter:
    def __init__(self, ids): self._ids = list(ids)
    def __aiter__(self): return self
    async def __anext__(self):
        if not self._ids:
            raise StopAsyncIteration
        return _Msg(self._ids.pop(0))


class _Client:
    def __init__(self, n_msgs, bad_ids=()):
        self.n = n_msgs
        self.bad = set(bad_ids)
        self.batches: list[list[int]] = []
        self.iter_limit = "unset"

    async def connect(self): return None
    async def disconnect(self): return None

    def iter_messages(self, peer, limit=None):
        self.iter_limit = limit
        ids = list(range(self.n, 0, -1))
        return _Iter(ids[:limit] if limit else ids)

    async def delete_messages(self, peer, ids, revoke=True):
        self.batches.append(list(ids))
        if self.bad & set(ids):
            raise RuntimeError("MESSAGE_DELETE_FORBIDDEN")
        return [_Affected(len(ids))]


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(account_manager, "_make_client", lambda s, a=None: client)

    async def _peer(c, ref, h=0): return ("peer", ref, h)
    monkeypatch.setattr(account_manager, "_resolve_channel_peer", _peer)


def test_clear_all_posts_in_batches_of_100(monkeypatch):
    client = _Client(250)
    _patch_client(monkeypatch, client)
    r = asyncio.run(account_manager.clear_channel_posts("s", 111, access_hash=9))
    assert r == {"ok": True, "deleted": 250}
    assert [len(b) for b in client.batches] == [100, 100, 50]
    assert client.iter_limit is None, "limit=0 означает все посты"


def test_clear_last_n_only(monkeypatch):
    client = _Client(250)
    _patch_client(monkeypatch, client)
    r = asyncio.run(account_manager.clear_channel_posts("s", 111, limit=30))
    assert r["deleted"] == 30 and client.iter_limit == 30
    assert client.batches == [list(range(250, 220, -1))]


def test_one_undeletable_post_does_not_keep_the_rest(monkeypatch):
    client = _Client(5, bad_ids={3})
    _patch_client(monkeypatch, client)
    r = asyncio.run(account_manager.clear_channel_posts("s", 111))
    # Пачка упала из-за №3 — остальные дочищены поштучно.
    assert r["ok"] is True and r["deleted"] == 4


def test_destroy_names_the_reason(monkeypatch):
    class ChatAdminRequiredError(Exception):
        pass

    class _C(_Client):
        async def __call__(self, req):
            raise ChatAdminRequiredError("CHAT_ADMIN_REQUIRED")
    _patch_client(monkeypatch, _C(0))
    r = asyncio.run(account_manager.destroy_channel("s", 111, access_hash=9))
    assert r["ok"] is False and "создатель" in r["error"]


# ── Маршруты мини-аппа ────────────────────────────────────────────────────

class _ApiPool:
    def __init__(self):
        self.fetched: list[str] = []

    async def fetchrow(self, q, *a):
        if "FROM managed_channels" in q:
            return {"channel_id": a[0], "title": "Новости", "acc_id": 5,
                    "access_hash": 9, "username": "news", "type": "channel"}
        return None

    async def fetch(self, q, *a):
        self.fetched.append(q)
        if "FROM managed_channels" in q:
            return [{"channel_id": i, "title": f"К{i}", "acc_id": 5, "access_hash": 9,
                     "username": "", "type": "channel"} for i in a[1]]
        if "FROM operation_queue" in q:
            return [{"id": 77, "op_type": "quick_post", "status": "pending",
                     "label": "Быстрый пост", "scheduled_for": None, "created_at": None,
                     "text": "Привет", "text_to_post": None, "repeat_interval_min": 1440,
                     "repeat_count": None, "channels": 1}]
        return []

    async def fetchval(self, q, *a): return 3
    async def execute(self, q, *a): return "OK"


async def _call(pool, monkeypatch, method, path, payload=None):
    monkeypatch.setattr(M, "_get_uid", lambda request: UID)
    from services import security as _sec
    _sec._rate_limiter._requests.clear()
    app = web.Application()
    M.setup_routes(app, pool)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as s:
            kw = {}
            if payload is not None:
                kw = {"data": json.dumps(payload), "headers": {"Content-Type": "application/json"}}
            async with s.request(method, f"http://127.0.0.1:{port}{path}", **kw) as r:
                return r.status, await r.json()
    finally:
        await runner.cleanup()


def _capture_submit(monkeypatch):
    sent = []

    async def _submit(pool, uid, op_type, params, **kw):
        sent.append((op_type, params, kw))
        return 501
    monkeypatch.setattr(M._obus, "submit", _submit)
    return sent


def test_route_refuses_delete_without_word(monkeypatch):
    sent = _capture_submit(monkeypatch)
    st, d = asyncio.run(_call(_ApiPool(), monkeypatch, "POST",
                              "/api/miniapp/channel/111/manage", {"op": "delete"}))
    assert st == 400 and "УДАЛИТЬ" in d["error"]
    assert not sent


def test_route_delete_single_channel(monkeypatch):
    sent = _capture_submit(monkeypatch)
    st, d = asyncio.run(_call(_ApiPool(), monkeypatch, "POST",
                              "/api/miniapp/channel/111/manage",
                              {"op": "delete", "confirm": "удалить"}))
    assert st == 200 and d["op_id"] == 501
    op_type, params, _ = sent[0]
    assert op_type == "bulk_chan_exec" and params["op"] == "chan_delete"
    assert params["confirmed"] is True
    assert params["channel_acc_pairs"][0]["type"] == "channel"


def test_route_mass_clear(monkeypatch):
    sent = _capture_submit(monkeypatch)
    pool = _ApiPool()
    st, d = asyncio.run(_call(pool, monkeypatch, "POST", "/api/miniapp/channels/mass",
                              {"op": "clear", "channel_ids": [1, 2], "confirm": "УДАЛИТЬ",
                               "clear_limit": 100}))
    assert st == 200 and d["count"] == 2
    assert sent[0][1]["op"] == "chan_clear" and sent[0][1]["clear_limit"] == 100
    # Опечатка в числе постов не превращается в «стереть всё».
    st, d = asyncio.run(_call(pool, monkeypatch, "POST", "/api/miniapp/channels/mass",
                              {"op": "clear", "channel_ids": [1], "confirm": "УДАЛИТЬ",
                               "clear_limit": "5О"}))
    assert st == 400 and len(sent) == 1


def test_route_autoposts_list(monkeypatch):
    pool = _ApiPool()
    st, d = asyncio.run(_call(pool, monkeypatch, "GET",
                              "/api/miniapp/channels/autoposts?channel_id=111"))
    assert st == 200 and d["autoposts"][0]["id"] == 77
    assert d["autoposts"][0]["text"] == "Привет"
    q = pool.fetched[-1]
    assert "repeat_interval_min" in q and "channel_ref" in q


def test_autopost_keeps_groups_when_target_all(monkeypatch):
    sent = _capture_submit(monkeypatch)
    st, _ = asyncio.run(_call(_ApiPool(), monkeypatch, "POST", "/api/miniapp/mass_publish",
                              {"text": "Пост", "channel_ids": [5], "repeat_interval_min": 60,
                               "repeat_count": 3, "target": "all"}))
    assert st == 200
    _, params, _ = sent[0]
    assert params["target"] == "all" and params["repeat_interval_min"] == 60
    assert params["repeat_count"] == 3


# ── Интерфейс ─────────────────────────────────────────────────────────────

def test_ui_wires_new_actions():
    assert '<script src="screens/channel_manage.js"></script>' in HTML
    m = re.search(r'<div id="chSelBar".*?</div>\s*</div>\s*</div>', HTML, re.DOTALL)
    assert m
    bar = m.group(0)
    for call in ("openChDestructive('delete'", "openChDestructive('clear'",
                 "openChAutopost(", "runChLeave(", "bulkDeleteChannels("):
        assert call in bar, call
    i = HTML.index("function buildChDetail")
    card = HTML[i:i + 6000]
    assert "Удалить в Telegram" in card and "openChAutopost([" in card
    assert "Убрать из системы" in card
    assert "chdAutoposts" in card


def test_ui_destructive_button_locked_until_word_typed():
    # Кнопка выключена изначально, включается только словом, и отправка
    # ещё раз сверяет слово и шлёт его серверу.
    assert re.search(r'id="cmBtn"[^>]*disabled', JS)
    assert "btn.disabled = v !== CM_CONFIRM_WORD" in JS
    m = re.search(r"async function submitChDestructive\(\)\s*\{(.*?)\n\}", JS, re.DOTALL)
    body = m.group(1)
    assert body.index("CM_CONFIRM_WORD") < body.index("_cmSubmit(")
    assert "confirm: confirmWord" in body


def test_card_role_matches_channel_list():
    # Создателю — «Удалить в Telegram», чужому — «Покинуть».
    assert M.managed_channel_role({"is_creator": True, "is_admin": True}) == "owner"
    assert M.managed_channel_role({"is_creator": False, "is_admin": True}) == "admin"
    assert M.managed_channel_role({"is_creator": None, "is_admin": False}) == "member"
    assert M.managed_channel_role({"is_creator": None, "is_admin": None}) == "unknown"
