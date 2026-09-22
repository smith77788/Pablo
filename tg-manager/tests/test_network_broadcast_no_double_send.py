"""Сетевая рассылка не отправляет одно и то же дважды.

Исполнитель заводит по рассылке на каждого бота сети. Тело запуска было
выписано в четырёх ветках сегментов слово в слово, и обе ошибки, которые здесь
закрываются, жили сразу во всех четырёх копиях.

1. ПОВТОР ОПЕРАЦИИ = ВТОРАЯ ОТПРАВКА. Операцию можно повторить: упал воркер на
   середине списка ботов, сработал потолок прогона, владелец нажал «повторить».
   Прошлый прогон нигде не оставлял следа, какие боты уже запущены, поэтому
   повтор заводил по каждому боту НОВУЮ рассылку с новым id. Журнал доставок
   ведётся по id рассылки, значит он второй рассылке не указ: вся аудитория уже
   обработанных ботов получала сообщение второй раз.

2. СЕГМЕНТ ПРЕВРАЩАЛСЯ ВО ВСЮ АУДИТОРИЮ. `create_broadcast` вызывался без
   `target_user_ids`, а именно его читает возобновление рассылок после
   рестарта. Поэтому прерванная рассылка по «потерянным за 30 дней» или «по
   языку» догонялась после рестарта по ВСЕЙ аудитории бота — люди, которых в
   сегменте не было, получали сообщение.

Обратная сторона (её тоже стережём): для ПОЛНОЙ аудитории список сохранять
нельзя, иначе состав подписчиков замораживается на момент запуска.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services import op_worker, broadcaster
from database import db as _db


class _FakePool:
    def __init__(self, op_status="running"):
        self.op_status = op_status
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        if "FROM bot_users" in query:
            return [{"user_id": 100}, {"user_id": 101}]
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": self.op_status}
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    def wrote(self, needle):
        return [a for q, a in self.executed if needle in q]


@pytest.fixture(autouse=True)
def _clean():
    op_worker._cancel_cache.clear()


@pytest.fixture
def _stubs(monkeypatch):
    calls = {"created": [], "started": [], "cancelled": []}

    async def _get_bots(pool, owner):
        return [{"bot_id": 1, "token": "1:AA"}, {"bot_id": 2, "token": "2:BB"}]

    async def _create(pool, bot_id, text, total, owner, **kw):
        calls["created"].append((bot_id, kw.get("target_user_ids")))
        return 900 + bot_id

    async def _inactive(pool, bot_id, days_from, days_to):
        return [200, 201]

    async def _unique(pool, owner):
        return [{"bot_id": 1, "user_id": 300, "token": "1:AA"},
                {"bot_id": 2, "user_id": 301, "token": "2:BB"}]

    monkeypatch.setattr(_db, "get_bots", _get_bots)
    monkeypatch.setattr(_db, "create_broadcast", _create)
    monkeypatch.setattr(_db, "get_inactive_user_ids", _inactive)
    monkeypatch.setattr(_db, "get_unique_network_users", _unique)
    monkeypatch.setattr(broadcaster, "start",
                        lambda *a, **kw: calls["started"].append(a[2]))
    monkeypatch.setattr(broadcaster, "cancel",
                        lambda bid: calls["cancelled"].append(bid) or True)
    return calls


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec(pool, params):
    return _run(op_worker._exec_network_broadcast(pool, None, 42, 777, dict(params)))


@pytest.mark.parametrize("segment", ["lang", "cold_all", "lost_all", "unique"])
def test_subset_segments_save_target_list(_stubs, segment):
    pool = _FakePool()
    _exec(pool, {"text": "привет", "segment": segment, "lang": "ru"})
    assert _stubs["created"], f"сегмент {segment} не завёл ни одной рассылки"
    for bot_id, target in _stubs["created"]:
        assert target, (
            f"сегмент {segment}: список получателей не сохранён — после рестарта "
            f"рассылка догонится по ВСЕЙ аудитории бота"
        )


def test_full_audience_keeps_target_null(_stubs):
    pool = _FakePool()
    _exec(pool, {"text": "привет", "segment": "all_each"})
    assert _stubs["created"]
    for bot_id, target in _stubs["created"]:
        assert target is None, (
            "полная аудитория обязана оставаться NULL: сохранённый список "
            "заморозил бы состав подписчиков на момент запуска"
        )


def test_launched_bots_are_pinned_into_params(_stubs):
    pool = _FakePool()
    _exec(pool, {"text": "привет", "segment": "all_each"})
    pinned = pool.wrote("SET done_items=done_items+1, params")
    assert pinned, (
        "запущенные боты не записаны в params — повтор операции отправит их "
        "аудиториям сообщение второй раз"
    )
    last = json.loads(pinned[-1][0])
    assert last["launched_bot_ids"] == [1, 2]
    assert last["launched_bc_ids"] == [901, 902]


def test_repeat_run_skips_already_launched_bots(_stubs):
    pool = _FakePool()
    res = _exec(pool, {"text": "привет", "segment": "all_each",
                       "launched_bot_ids": [1], "launched_bc_ids": [901]})
    assert [b for b, _ in _stubs["created"]] == [2], (
        "бот, по которому рассылка уже заведена, не должен получать вторую"
    )
    assert res["status"] == "done"
    # Пропущенный бот всё равно считается запущенным: прогресс и итог не должны
    # проседать только потому, что работа сделана прошлым прогоном.
    assert res["bots_started"] == 2


def test_cancel_reaches_broadcasts_from_previous_run(_stubs):
    pool = _FakePool(op_status="cancelled")
    res = _exec(pool, {"text": "привет", "segment": "all_each",
                       "launched_bc_ids": [901]})
    assert res["status"] == "cancelled"
    assert _stubs["cancelled"] == [901], (
        "отмена обязана доставать и рассылки, запущенные прошлым прогоном — "
        "иначе они продолжают отправку после «Отменить»"
    )
