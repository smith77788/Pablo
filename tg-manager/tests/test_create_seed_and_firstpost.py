"""Автопосев + первый контент при массовом создании каналов.

Задача владельца — убрать ручную рутину: настроить ДО старта, чтобы каждый
созданный ресурс сразу получил первый пост (и закреп) и подписчиков своим
флотом. Проверяем на заглушках, что цепочка реально срабатывает: пост
отправляется, автопосев уходит операцией boost_subscribers, и в посев НЕ
попадает аккаунт-создатель (заходят ДРУГИЕ аккаунты).
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker


class _Pool:
    async def execute(self, q, *a):
        return "OK"
    async def fetch(self, q, *a):
        return []
    async def fetchval(self, q, *a):
        return 0
    async def fetchrow(self, q, *a):
        return None


def _install(monkeypatch, submitted, posts):
    accs = [{"id": 1, "session_str": "s1", "first_name": "A"},
            {"id": 2, "session_str": "s2", "first_name": "B"}]

    async def _select_all_active(pool, owner_id, **kw):
        return [dict(a) for a in accs]
    async def _claim(ids):
        return list(ids)
    async def _release(ids):
        return None
    async def _cancelled(pool, op_id):
        return False

    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)

    from services import account_manager, session_simulator, operation_bus
    async def _create_channel(session, title, about="", megagroup=False, _acc=None):
        return {"channel_id": 1000 + int(_acc["id"]), "access_hash": 7, "type": "channel"}
    async def _set_username(session, ch_id, cand, _acc=None):
        return ""                                   # успех: @ ставится с первого раза
    async def _post(session, ch_id, text, **kw):
        posts.append({"ch_id": ch_id, "text": text})
        return {"msg_id": 1}
    async def _pin(session, ch_id, **kw):
        return {"pinned_msg_id": 1}
    monkeypatch.setattr(account_manager, "create_channel", _create_channel)
    monkeypatch.setattr(account_manager, "set_channel_username", _set_username)
    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(account_manager, "pin_last_channel_post", _pin)
    monkeypatch.setattr(account_manager, "is_dead_session_error", lambda *_a, **_k: False)

    async def _typing(*a, **k):
        return None
    monkeypatch.setattr(session_simulator, "typing_delay", _typing)
    monkeypatch.setattr(session_simulator, "chaos_factor", lambda: 0.0)

    async def _submit(pool, owner_id, op_type, params, **kw):
        submitted.append({"op_type": op_type, "params": params})
        return 999
    monkeypatch.setattr(operation_bus, "submit", _submit)


def test_seed_and_firstpost_fire(monkeypatch):
    submitted, posts = [], []
    _install(monkeypatch, submitted, posts)
    params = {
        "account_ids": [1, 2], "title": "Dostavka Moskva", "channel_count": 1,
        "name_mode": "keywords", "username_template": "Dostavka_Moskva",
        "first_post": "Привет, это первый пост", "pin_first_post": True,
        "seed_count": 1, "engage_count": 1, "bulk_pacing": "fast",
    }
    res = asyncio.run(op_worker._exec_bulk_create_channels_multi(_Pool(), None, 1, 42, params))
    assert res["created"] == 2

    # первый пост ушёл в каждый канал
    assert len(posts) == 2
    assert all("первый пост" in p["text"] for p in posts)

    # автопосев ушёл операцией boost_subscribers по каждому каналу
    seeds = [s for s in submitted if s["op_type"] == "boost_subscribers"]
    assert len(seeds) == 2

    # оживление первого поста: реакции + просмотры по каждому каналу
    assert len([s for s in submitted if s["op_type"] == "boost_reactions"]) == 2
    assert len([s for s in submitted if s["op_type"] == "boost_views"]) == 2
    _react = next(s for s in submitted if s["op_type"] == "boost_reactions")
    assert _react["params"].get("msg_id") == 1     # msg_id первого поста
    # создатель НЕ участвует в посеве своего же канала (заходят другие)
    # канал создателя #1 → посев аккаунтом [2]; создателя #2 → [1]
    seeded_sets = sorted(tuple(s["params"]["account_ids"]) for s in seeds)
    assert seeded_sets == [(1,), (2,)]


def test_no_seed_when_zero(monkeypatch):
    submitted, posts = [], []
    _install(monkeypatch, submitted, posts)
    params = {
        "account_ids": [1, 2], "title": "Dostavka Moskva", "channel_count": 1,
        "name_mode": "keywords", "seed_count": 0, "bulk_pacing": "fast",
    }
    asyncio.run(op_worker._exec_bulk_create_channels_multi(_Pool(), None, 1, 42, params))
    assert not [s for s in submitted if s["op_type"] == "boost_subscribers"]
    assert posts == []          # first_post не задан — ничего не постим
