"""Повтор рассылки в личные не пишет человеку второй раз.

Повтор операции — событие штатное: автоматический после сетевого сбоя, сброс
зависшей операции сторожем, ручной перезапуск. Он поднимает исполнителя заново
с done_items=0 и ведёт по ВСЕМУ списку получателей сначала.

Для личных сообщений это худший из возможных дублей. Два одинаковых ЛС одному
человеку — самая заметная спам-сигнатура, и `_exec_bulk_dm_adhoc` от неё уже
защищается ВНУТРИ одного прогона, схлопывая дубли во входном списке. На оси
повтора защиты не было: пер-целевой журнал операция писала, но никто его не
читал. `_exec_self_promo_blast` не писал и журнала.

Повторять нельзя два разных исхода, и по разным причинам:
  * `ok`   — человек получит ВТОРОЕ одинаковое сообщение;
  * `skip` — получатель недостижим навсегда (закрытые ЛС, блок, удалён), и
    новая неудачная попытка контакта копит давление к PeerFlood.
Ровно так отбирает получателей dm_engine для кампаний (dm_campaign_log со
статусами sent/blocked/skip) — здесь тот же критерий.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker, account_manager


class _FakePool:
    def __init__(self, settled=()):
        # settled: последовательность (target, status)
        self.settled = list(settled)
        self.logged: list[tuple] = []
        self.done_items = 0

    async def fetch(self, query, *args):
        if "FROM operation_log" in query:
            return [{"target": t, "status": st} for t, st in self.settled]
        if "FROM bot_users bu" in query:
            return [{"user_id": 1, "bot_id": 500, "token": "t"},
                    {"user_id": 2, "bot_id": 500, "token": "t"}]
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        if "FROM self_promo_templates" in query:
            return {"id": 3, "title": "T", "content": "текст",
                    "cta_text": None, "cta_url": None}
        return None

    async def execute(self, query, *args):
        if "INSERT INTO operation_log" in query:
            self.logged.append((args[2], args[3]))
        if "SET done_items=done_items+1" in query:
            self.done_items += 1
        return "UPDATE 1"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── bulk_dm_adhoc ───────────────────────────────────────────────────────────

@pytest.fixture
def _dm(monkeypatch):
    sent: list[str] = []

    async def _send(session, username, text, **kw):
        sent.append(username)
        return {"ok": True}

    async def _select(pool, owner_id, include_ids=None, min_trust_score=0.0):
        return [{"id": 11, "session_str": "s", "first_name": "A", "phone": "+1"}]

    async def _claim(ids):
        return list(ids)

    async def _release(ids):
        return None

    async def _quar(pool, acc_id):
        return False

    async def _opted(pool, owner_id):
        return set()

    _real_sleep = asyncio.sleep

    async def _no_sleep(*_a, **_k):
        await _real_sleep(0)

    monkeypatch.setattr(account_manager, "send_dm", _send)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quar)
    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    from services import contact_opt_out
    monkeypatch.setattr(contact_opt_out, "load_opted_out", _opted)
    op_worker._cancel_cache.clear()
    return {"sent": sent}


_DM_PARAMS = {"account_ids": [11], "usernames": ["@vasya", "@petya"], "text": "привет"}


def test_dm_first_run_writes_journal(_dm):
    pool = _FakePool()
    res = _run(op_worker._exec_bulk_dm_adhoc(pool, None, 42, 777, dict(_DM_PARAMS)))
    assert len(_dm["sent"]) == 2
    assert res["ok"] == 2
    assert [st for _t, st in pool.logged] == ["ok", "ok"]


def test_dm_retry_skips_already_sent(_dm):
    pool = _FakePool(settled=[("@vasya", "ok")])
    res = _run(op_worker._exec_bulk_dm_adhoc(pool, None, 42, 777, dict(_DM_PARAMS)))
    assert _dm["sent"] == ["@petya"], (
        "повтор написал человеку, которому сообщение уже ушло — второе "
        "одинаковое ЛС это самая заметная спам-сигнатура"
    )
    assert res["ok"] == 2, "отправленное прошлым прогоном остаётся засчитанным"
    assert pool.done_items == 2


def test_dm_retry_skips_unreachable(_dm):
    pool = _FakePool(settled=[("@vasya", "skip")])
    res = _run(op_worker._exec_bulk_dm_adhoc(pool, None, 42, 777, dict(_DM_PARAMS)))
    assert _dm["sent"] == ["@petya"], (
        "повтор снова ломится к недостижимому получателю — череда неудачных "
        "попыток контакта ведёт к PeerFlood"
    )
    assert res["skipped"] == 1, "недостижимый остаётся пропуском, а не успехом"
    assert res["ok"] == 1


def test_dm_journal_key_ignores_at_and_case(_dm):
    """Журнал хранит то, что дал пользователь; сверка обязана быть устойчивой."""
    pool = _FakePool(settled=[("Vasya", "ok")])
    _run(op_worker._exec_bulk_dm_adhoc(pool, None, 42, 777, dict(_DM_PARAMS)))
    assert _dm["sent"] == ["@petya"]


# ── self_promo_blast ────────────────────────────────────────────────────────

@pytest.fixture
def _promo(monkeypatch):
    sent: list[int] = []

    class _FakeBotApi:
        def __init__(self, token=None):
            self.session = self

        async def send_message(self, user_id, text, **kw):
            sent.append(user_id)

        async def close(self):
            return None

        async def get_me(self):
            return None

    import aiogram
    monkeypatch.setattr(aiogram, "Bot", _FakeBotApi)
    from services import token_vault
    monkeypatch.setattr(token_vault, "decrypt_token", lambda t: t or "")
    _real_sleep = asyncio.sleep

    async def _no_sleep(*_a, **_k):
        await _real_sleep(0)

    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    op_worker._cancel_cache.clear()
    return {"sent": sent}


class _PromoBot:
    async def get_me(self):
        return None


def test_promo_first_run_writes_journal(_promo):
    pool = _FakePool()
    res = _run(op_worker._exec_self_promo_blast(
        pool, _PromoBot(), 42, 777, {"template_id": 3}))
    assert _promo["sent"] == [1, 2]
    assert [t for t, _st in pool.logged] == ["500:1", "500:2"], (
        "ключ журнала обязан различать одного и того же человека у разных ботов"
    )
    assert res["ok"] == 2


def test_promo_retry_skips_already_sent(_promo):
    pool = _FakePool(settled=[("500:1", "ok")])
    res = _run(op_worker._exec_self_promo_blast(
        pool, _PromoBot(), 42, 777, {"template_id": 3}))
    assert _promo["sent"] == [2], (
        "повтор отправил промо человеку, который его уже получил"
    )
    assert res["ok"] == 2
    assert pool.done_items == 2


def test_promo_cancel_is_not_reported_as_done(_promo, monkeypatch):
    async def _cancelled(pool, op_id):
        return True

    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)
    pool = _FakePool()
    res = _run(op_worker._exec_self_promo_blast(
        pool, _PromoBot(), 42, 777, {"template_id": 3}))
    assert res["status"] == "cancelled", (
        "остановленная владельцем рассылка рапортовала успешное завершение"
    )
    assert _promo["sent"] == []
