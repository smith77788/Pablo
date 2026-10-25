"""Массовое изменение профиля ботов: имя, описание, приветствие — у многих/всех.

Жалоба владельца: Infragram не умеет менять описание/приветствие/имя сразу у
нескольких или всех заданных ботов. Операция bulk_bot_profile меняет за один
проход имя/описание/короткое описание (Bot API) и приветствие на /start
(локальное правило auto_replies) у выбранных (или всех) ботов владельца.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
from contextlib import asynccontextmanager

import pytest

from services import op_worker, operation_bus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Регистрация операции ──────────────────────────────────────────────────────

def test_op_registered_in_dispatch_and_registry():
    assert op_worker.handler_for("bulk_bot_profile") is op_worker._exec_bulk_bot_profile
    assert "bulk_bot_profile" in operation_bus.OP_REGISTRY
    meta = operation_bus.OP_REGISTRY["bulk_bot_profile"]
    assert meta.get("min_plan") and meta.get("description")


def test_route_registered():
    src = open(os.path.join(ROOT, "services", "mini_app_bots.py"), encoding="utf-8").read()
    assert '"/api/miniapp/bots/bulk_profile"' in src
    assert "bots_bulk_profile" in src


def test_handler_resumes_so_repeat_is_safe():
    """Храповик повторяемых операций: журналит И умеет возобновляться."""
    body = inspect.getsource(op_worker._exec_bulk_bot_profile)
    assert "INSERT INTO operation_log" in body, "нет по-ботного журнала"
    assert "completed_targets(" in body, "повтор не пропускает уже сделанных ботов"


# ── Поведение исполнителя ─────────────────────────────────────────────────────

class _Conn:
    def __init__(self, log):
        self.log = log

    async def execute(self, q, *a):
        self.log.append((q, a))

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()


class _Pool:
    def __init__(self):
        self.calls = []

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "INSERT 1"

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self.calls)


def _patch_common(monkeypatch, bots, api_result):
    async def fake_fetch_bots(pool, query, *args):
        return list(bots)
    import database.db as _db
    monkeypatch.setattr(_db, "fetch_bots", fake_fetch_bots)

    async def fake_completed(pool, op_id):
        return set()
    monkeypatch.setattr(op_worker, "completed_targets", fake_completed)

    async def fake_cancelled(pool, op_id):
        return False
    monkeypatch.setattr(op_worker, "_is_cancelled", fake_cancelled)

    async def fake_safe_exec(pool, query, *args, **kw):
        pool.calls.append((query, args))
        return "UPDATE 1"
    monkeypatch.setattr(op_worker, "_safe_execute", fake_safe_exec)

    calls = []
    from services import bot_api

    async def fake_call(sess, token, method, **params):
        calls.append((token, method, params))
        return dict(api_result)
    monkeypatch.setattr(bot_api, "_call", fake_call)

    # не спать по секунде на каждого бота в тесте
    async def _nosleep(*a, **k):
        return None
    monkeypatch.setattr(op_worker.asyncio, "sleep", _nosleep)
    return calls


@pytest.mark.asyncio
async def test_applies_only_requested_fields_to_selected_bots(monkeypatch):
    bots = [{"id": 1, "bot_id": 111, "token": "T1"},
            {"id": 2, "bot_id": 222, "token": "T2"}]
    api_calls = _patch_common(monkeypatch, bots, {"ok": True})
    pool = _Pool()
    res = await op_worker._exec_bulk_bot_profile(
        pool, None, 7, 99,
        {"bot_ids": [111, 222], "name": "Новое имя", "greeting": "Привет!"})
    assert res["status"] == "done" and res["ok"] == 2 and res["failed"] == 0
    # имя задано → setMyName на каждого бота; описание/короткое НЕ заданы → их нет
    methods = [m for _, m, _ in api_calls]
    assert methods.count("setMyName") == 2
    assert "setMyDescription" not in methods and "setMyShortDescription" not in methods
    # приветствие → замена правила start в auto_replies (delete+insert) на бота
    sql = " ".join(q for q, _ in pool.calls)
    assert "DELETE FROM auto_replies" in sql and "trigger_type='start'" in sql
    assert "INSERT INTO auto_replies" in sql
    # по-ботный журнал 'ok'
    assert sql.count("INSERT INTO operation_log") == 2


@pytest.mark.asyncio
async def test_greeting_only_makes_no_bot_api_calls(monkeypatch):
    bots = [{"id": 1, "bot_id": 111, "token": "T1"}]
    api_calls = _patch_common(monkeypatch, bots, {"ok": True})
    pool = _Pool()
    res = await op_worker._exec_bulk_bot_profile(
        pool, None, 7, 99, {"bot_ids": [111], "greeting": "Здравствуйте"})
    assert res["status"] == "done" and res["ok"] == 1
    assert api_calls == [], "приветствие — это не вызов Bot API"


@pytest.mark.asyncio
async def test_dead_token_is_a_russian_per_bot_error(monkeypatch):
    bots = [{"id": 5, "bot_id": 555, "token": "DEAD"}]
    _patch_common(monkeypatch, bots, {"ok": False, "error_code": 401,
                                      "description": "Unauthorized"})
    pool = _Pool()
    res = await op_worker._exec_bulk_bot_profile(
        pool, None, 7, 99, {"bot_ids": [555], "name": "Имя"})
    assert res["failed"] == 1 and res["ok"] == 0
    # в журнал ушла строка 'error' с РУССКОЙ причиной, а не англ. текстом Telegram
    err_inserts = [(q, a) for q, a in pool.calls
                   if "operation_log" in q and "'error'" in q]
    assert err_inserts, "нет записи об ошибке в журнале"
    assert any("Токен отозван" in str(a) for _, a in err_inserts)


@pytest.mark.asyncio
async def test_no_fields_fails_cleanly(monkeypatch):
    bots = [{"id": 1, "bot_id": 111, "token": "T1"}]
    _patch_common(monkeypatch, bots, {"ok": True})
    pool = _Pool()
    res = await op_worker._exec_bulk_bot_profile(pool, None, 7, 99, {"bot_ids": [111]})
    assert res["status"] == "failed"


@pytest.mark.asyncio
async def test_no_matching_bots_fails(monkeypatch):
    _patch_common(monkeypatch, [], {"ok": True})
    pool = _Pool()
    res = await op_worker._exec_bulk_bot_profile(
        pool, None, 7, 99, {"bot_ids": [111], "name": "Имя"})
    assert res["status"] == "failed"


@pytest.mark.asyncio
async def test_already_done_bots_are_skipped_on_resume(monkeypatch):
    bots = [{"id": 1, "bot_id": 111, "token": "T1"},
            {"id": 2, "bot_id": 222, "token": "T2"}]
    api_calls = _patch_common(monkeypatch, bots, {"ok": True})

    async def done_first(pool, op_id):
        return {"1"}  # бот id=1 уже сделан прошлым прогоном
    monkeypatch.setattr(op_worker, "completed_targets", done_first)

    pool = _Pool()
    res = await op_worker._exec_bulk_bot_profile(
        pool, None, 7, 99, {"bot_ids": [111, 222], "name": "Имя"})
    assert res["status"] == "done"
    # по Telegram ударили только по второму боту (первый пропущен)
    tokens = [t for t, _, _ in api_calls]
    assert tokens == ["T2"], f"повтор ударил по уже сделанному боту: {tokens}"
