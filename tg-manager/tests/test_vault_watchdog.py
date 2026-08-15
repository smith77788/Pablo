"""Сторож «Хранилища» шлёт ОДНО предупреждение при зависании и не спамит.

Проверяем ветки _check_once: зависло+не уведомляли → шлём; уже уведомляли →
молчим; трафик вернулся → снимаем метку; свежее подключение без сообщений в
grace → не трогаем.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from services import vault_watchdog as w


def _ago(days):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.execs = []

    async def fetch(self, *a):
        return self._rows

    async def execute(self, sql, *args):
        self.execs.append((sql, args))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _row(**kw):
    base = {"connection_id": "c1", "owner_id": 1, "user_chat_id": 99,
            "created_at": _ago(30), "stale_notified_at": None, "last_at": None}
    base.update(kw)
    return base


def test_stale_and_not_notified_sends_once():
    pool = _FakePool([_row(last_at=_ago(7))])
    bot = _FakeBot()
    sent = _run(w._check_once(pool, bot))
    assert sent == 1
    assert bot.sent[0][0] == 99                       # в личный чат владельца
    # метка проставлена
    assert any("stale_notified_at=now()" in e[0] for e in pool.execs)


def test_already_notified_stays_silent():
    pool = _FakePool([_row(last_at=_ago(7), stale_notified_at=_ago(1))])
    bot = _FakeBot()
    sent = _run(w._check_once(pool, bot))
    assert sent == 0
    assert not bot.sent


def test_traffic_returned_clears_flag():
    pool = _FakePool([_row(last_at=_ago(0), stale_notified_at=_ago(3))])
    bot = _FakeBot()
    sent = _run(w._check_once(pool, bot))
    assert sent == 0
    assert any("stale_notified_at=NULL" in e[0] for e in pool.execs)


def test_fresh_connection_no_messages_within_grace_not_notified():
    pool = _FakePool([_row(created_at=_ago(1), last_at=None)])
    bot = _FakeBot()
    sent = _run(w._check_once(pool, bot))
    assert sent == 0


def test_old_connection_no_messages_notified():
    pool = _FakePool([_row(created_at=_ago(30), last_at=None)])
    bot = _FakeBot()
    sent = _run(w._check_once(pool, bot))
    assert sent == 1
