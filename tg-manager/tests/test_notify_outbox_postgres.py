"""Очередь доставки уведомлений на живой базе.

Заглушка пула SQL не исполняет, а у очереди всё держится на самом запросе:
потолок на получателя в INSERT, захват строк под FOR UPDATE SKIP LOCKED со
сдвигом срока (два процесса одну строку не берут), удаление доставленного.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import notify_outbox

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
UID = 994377

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM notification_outbox WHERE user_id=$1", UID))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM notification_outbox"))
    yield


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent: list[str] = []

    async def send_message(self, uid, text, **kw):
        if self.fail:
            raise RuntimeError("Bad Gateway")
        self.sent.append(text)


def test_enqueue_then_deliver_when_due(pool):
    assert _run(notify_outbox.enqueue(pool, UID, "op_complete", "<b>итог</b>",
                                      error="Bad Gateway"))
    # Свежая строка не созрела (первая пауза ~30 с) — проход её не трогает.
    bot = _Bot()
    assert _run(notify_outbox.deliver_once(pool, bot)) == 0
    _run(pool.execute("UPDATE notification_outbox SET next_attempt_at = now()"))
    assert _run(notify_outbox.deliver_once(pool, bot)) == 1
    assert "итог" in bot.sent[0]
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM notification_outbox WHERE user_id=$1", UID)) == 0


def test_a_failed_attempt_is_rescheduled_not_lost(pool):
    _run(notify_outbox.enqueue(pool, UID, "op_complete", "x"))
    _run(pool.execute("UPDATE notification_outbox SET next_attempt_at = now()"))
    _run(notify_outbox.deliver_once(pool, _Bot(fail=True)))
    row = _run(pool.fetchrow(
        "SELECT attempts, next_attempt_at > now() AS later, last_error "
        "FROM notification_outbox WHERE user_id=$1", UID))
    assert row["attempts"] == 1 and row["later"] and "Bad Gateway" in row["last_error"]


def test_two_processes_do_not_take_the_same_row(pool):
    _run(notify_outbox.enqueue(pool, UID, "op_complete", "x"))
    _run(pool.execute("UPDATE notification_outbox SET next_attempt_at = now()"))

    async def _both():
        return await asyncio.gather(notify_outbox._claim(pool),
                                    notify_outbox._claim(pool))

    a, b = _run(_both())
    assert len(a) + len(b) == 1


def test_per_recipient_cap(pool, monkeypatch):
    monkeypatch.setattr(notify_outbox, "_PER_USER_CAP", 2)
    assert _run(notify_outbox.enqueue(pool, UID, "p", "1"))
    assert _run(notify_outbox.enqueue(pool, UID, "p", "2"))
    assert not _run(notify_outbox.enqueue(pool, UID, "p", "3"))
