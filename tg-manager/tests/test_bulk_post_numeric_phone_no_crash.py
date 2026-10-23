"""Публикация/профиль не падают, если phone аккаунта пришёл из БД ЧИСЛОМ.

Жалоба владельца: «Массовая публикация в канал» и «Каналы (about)» обрывались с
'int' object has no attribute 'replace' — операция «Частично», успело 1 (падало на
ВТОРОМ аккаунте, когда у него нет first_name, а phone хранится числом).

Корень: label = html.escape(acc.get("first_name") or acc.get("phone") or ...).
html.escape зовёт s.replace(...), а на int это AttributeError. str() лечит.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import op_worker, account_manager
from tests.test_post_ops_idempotent_retry import _AccPool, _stubs  # noqa: F401


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def _accounts_numeric_phone(monkeypatch):
    async def _select(pool, owner_id, include_ids=None, min_trust_score=0.0):
        # второй аккаунт без first_name и с ЧИСЛОВЫМ phone — ровно прод-случай
        return [{"id": 11, "session_str": "s1", "first_name": "A", "phone": "+1"},
                {"id": 12, "session_str": "s2", "first_name": None, "phone": 79990001122}]
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select)


def test_bulk_post_survives_numeric_phone(_stubs, _accounts_numeric_phone):
    pool = _AccPool()
    res = _run(op_worker._exec_bulk_post_to_channel(
        pool, None, 261, 777,
        {"account_ids": [11, 12], "channel_ref": -1001234567890,
         "text_to_post": "привет", "bulk_access_hash": 55}))
    assert res["status"] == "done", f"публикация не должна падать на числовом phone: {res}"
    assert res["ok"] == 2


def test_label_builders_coerce_to_str():
    """Храповик: оба label-билдера оборачивают поля аккаунта в str() перед escape."""
    src = inspect.getsource(op_worker._exec_bulk_post_to_channel)
    assert 'str(acc.get("first_name") or acc.get("phone") or acc["id"])' in src
    src2 = inspect.getsource(op_worker._exec_bulk_update_profile)
    assert 'str(acc.get("first_name") or acc.get("phone") or acc["id"])' in src2
