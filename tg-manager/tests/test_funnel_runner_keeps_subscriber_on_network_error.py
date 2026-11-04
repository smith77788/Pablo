"""Старый исполнитель воронок не выкидывает подписчика из-за сбоя сети.

ЧТО БЫЛО. `funnel_runner.run_once` (путь FUNNELS_ENGINE=legacy, он же откат
с движка сценариев) на ЛЮБОМ неуспехе отправки, кроме флуда, помечал подписку
`dropped=true`: «бот заблокирован или аккаунт удалён». Сбой сети или 5xx
Telegram навсегда выкидывал человека из воронки и портил счётчик выбывших.
Выбывает теперь только тот, кто ушёл сам (`bot_api.recipient_is_gone`) — так
же, как в рассылке и автоворонке.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, query, *args):
        self.executed.append(query)
        return "UPDATE 1"


def _once(monkeypatch, category):
    from database import db
    from services import bot_api, brand_injection, funnel_runner, legacy_funnels

    async def _legacy_off(pool, kind):
        return False

    async def _due(pool):
        return [{"sub_id": 1, "funnel_id": 2, "user_id": 3, "current_step": 0,
                 "message_text": "Шаг", "delay_minutes": 0, "bot_id": 4,
                 "token": "t", "total_steps": 2}]

    async def _send(http, token, chat_id, text, **kw):
        return False, None, category

    async def _send_plain(http, token, chat_id, text, **kw):
        # Старый путь звал send_message без категории.
        return False, None

    async def _free(pool, bot_id):
        return False

    async def _advance(*a, **k):
        raise AssertionError("шаг не отправлен, а воронка сдвинулась")

    monkeypatch.setattr(legacy_funnels, "tick", _legacy_off)
    monkeypatch.setattr(db, "get_due_funnel_steps", _due)
    monkeypatch.setattr(db, "advance_funnel_step", _advance)
    monkeypatch.setattr(bot_api, "send_message_classified", _send)
    monkeypatch.setattr(bot_api, "send_message", _send_plain)
    monkeypatch.setattr(brand_injection, "is_free_tier", _free)
    pool = _Pool()
    asyncio.run(funnel_runner.run_once(pool, None))
    return pool.executed


@pytest.mark.parametrize("category", ["other", "unavailable", "flood", "unauthorized"])
def test_transient_failure_keeps_the_subscriber(monkeypatch, category):
    executed = _once(monkeypatch, category)
    assert not any("dropped=true" in q for q in executed), (
        f"сбой «{category}» выкинул подписчика из воронки")


@pytest.mark.parametrize("category", ["blocked", "deactivated", "bad_request"])
def test_gone_recipient_or_broken_step_still_drops(monkeypatch, category):
    executed = _once(monkeypatch, category)
    assert any("dropped=true" in q for q in executed)
