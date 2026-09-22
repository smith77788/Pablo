"""Повтор постинг-операции не публикует второй раз.

Повтор операции — штатное событие: автоматический после сетевого сбоя или
флуда, сброс зависшей операции сторожем, ручной перезапуск владельцем. Он
поднимает исполнителя заново с done_items=0, и тот идёт по ВСЕМУ списку целей
сначала. Для постинга это вторая публикация туда, где пост уже стоит: дубль у
подписчиков, сожжённый дневной лимит аккаунта и лишний повод для флуда — то
есть повтор после сетевого блипа сам по себе поднимал риск бана.

`mass_publish` и `bulk_join` эту защиту уже имели (`completed_targets` /
`completed_account_targets`), а три соседних исполнителя — нет, причём у них
вообще не было пер-целевого журнала, которого требует стандарт работы:

  * bulk_post_chans      — один аккаунт публикует в много каналов (ключ: канал);
  * group_announce       — один аккаунт шлёт объявление в много групп (ключ: группа);
  * bulk_post_to_channel — много аккаунтов публикуют в один канал (ключ: аккаунт,
    потому что единица работы здесь — пост ОТ аккаунта).
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker, account_manager
from database import db as _db


class _FakePool:
    def __init__(self, done_targets=(), rows=None):
        self.done_targets = set(done_targets)
        self.rows = rows or {}
        self.logged: list[tuple] = []
        self.done_items = 0

    async def fetch(self, query, *args):
        if "FROM operation_log" in query:
            return [{"target": t} for t in self.done_targets]
        if "FROM managed_channels" in query:
            return [{"id": 1, "channel_id": -100_1, "access_hash": 0, "username": ""},
                    {"id": 2, "channel_id": -100_2, "access_hash": 0, "username": ""}]
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        if "FROM tg_accounts WHERE id=" in query:
            return {"id": 7, "session_str": "s", "first_name": "acc", "phone": "+1",
                    "device_model": "", "system_version": "", "app_version": "",
                    "lang_code": "", "system_lang_code": "", "proxy_url": None}
        return None

    async def execute(self, query, *args):
        if "INSERT INTO operation_log" in query:
            self.logged.append((args[2], args[3]))
        if "SET done_items=done_items+1" in query:
            self.done_items += 1
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    posted: list = []

    async def _post(session, target, text, **kw):
        posted.append(target)
        return {"msg_id": 111}

    async def _claim_one(acc_id):
        return True

    async def _claim_many(ids):
        return list(ids)

    async def _release(ids):
        return None

    async def _quarantined(pool, acc_id):
        return False

    async def _dialogs(session, **kw):
        return [{"id": -100_1, "type": "megagroup", "access_hash": 0},
                {"id": -100_2, "type": "megagroup", "access_hash": 0}]

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(account_manager, "get_dialogs", _dialogs)
    monkeypatch.setattr(op_worker, "try_claim_account", _claim_one)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim_many)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quarantined)
    _real_sleep = asyncio.sleep

    async def _no_sleep(*_a, **_k):
        await _real_sleep(0)

    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    op_worker._cancel_cache.clear()
    return {"posted": posted}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── bulk_post_chans: один аккаунт → много каналов ───────────────────────────

def test_bulk_post_chans_first_run_posts_everywhere(_stubs):
    pool = _FakePool()
    res = _run(op_worker._exec_bulk_post_chans(
        pool, None, 42, 777, {"acc_id": 7, "channel_ids": [1, 2], "text": "привет"}))
    assert len(_stubs["posted"]) == 2
    assert res["ok"] == 2
    assert sorted(t for t, st in pool.logged if st == "ok") == ["-1001", "-1002"], (
        "успешная публикация обязана оставлять след в журнале целей, иначе "
        "повтору не по чему понять, что канал уже отработан"
    )


def test_bulk_post_chans_retry_skips_published(_stubs):
    pool = _FakePool(done_targets={"-1001"})
    res = _run(op_worker._exec_bulk_post_chans(
        pool, None, 42, 777, {"acc_id": 7, "channel_ids": [1, 2], "text": "привет"}))
    assert _stubs["posted"] == [-100_2], (
        "повтор опубликовал в канал, где пост уже стоит — дубль у подписчиков и "
        "сожжённый лимит аккаунта"
    )
    assert res["ok"] == 2, "пропущенный канал остаётся засчитанным"
    assert pool.done_items == 2, "прогресс не должен проседать из-за пропуска"


# ── group_announce: один аккаунт → много групп ──────────────────────────────

def test_group_announce_retry_skips_announced(_stubs):
    pool = _FakePool(done_targets={"-1001"})
    res = _run(op_worker._exec_group_announce(
        pool, None, 42, 777, {"acc_id": 7, "text": "привет"}))
    assert _stubs["posted"] == [-100_2], (
        "повтор отправил объявление в группу, которая его уже получила"
    )
    assert res["ok"] == 2
    assert pool.done_items == 2


def test_group_announce_first_run_logs_targets(_stubs):
    pool = _FakePool()
    _run(op_worker._exec_group_announce(
        pool, None, 42, 777, {"acc_id": 7, "text": "привет"}))
    assert sorted(t for t, st in pool.logged if st == "ok") == ["-1001", "-1002"]


# ── bulk_post_to_channel: много аккаунтов → один канал ──────────────────────

class _AccPool(_FakePool):
    async def fetch(self, query, *args):
        if "FROM operation_log" in query:
            return [{"target": t} for t in self.done_targets]
        return []


@pytest.fixture
def _accounts(monkeypatch):
    async def _select(pool, owner_id, include_ids=None, min_trust_score=0.0):
        return [{"id": 11, "session_str": "s1", "first_name": "A", "phone": "+1"},
                {"id": 12, "session_str": "s2", "first_name": "B", "phone": "+2"}]

    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select)


def test_bulk_post_to_channel_retry_skips_accounts(_stubs, _accounts):
    pool = _AccPool(done_targets={"11"})
    res = _run(op_worker._exec_bulk_post_to_channel(
        pool, None, 42, 777,
        {"account_ids": [11, 12], "channel_ref": "@chan", "text_to_post": "привет"}))
    assert len(_stubs["posted"]) == 1, (
        "повтор опубликовал в канал с аккаунта, который уже отправил пост"
    )
    assert res["ok"] == 2, "пропущенный аккаунт остаётся засчитанным"
    assert pool.done_items == 2


def test_bulk_post_to_channel_first_run_logs_accounts(_stubs, _accounts):
    pool = _AccPool()
    _run(op_worker._exec_bulk_post_to_channel(
        pool, None, 42, 777,
        {"account_ids": [11, 12], "channel_ref": "@chan", "text_to_post": "привет"}))
    assert sorted(t for t, st in pool.logged if st == "ok") == ["11", "12"], (
        "ключ журнала здесь — аккаунт: единица работы это пост ОТ аккаунта"
    )
