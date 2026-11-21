"""Отчёт владельцу переживает сбой Telegram, а тревога «Защита не записалась»
говорит только о том, что действительно сломалось.

Симптом из прода (21.11.2026), тревога владельцу:

    ⚠️ Защита не записалась
    • обмен темпом флота с базой не удался … — 2 раз(а)
    • аренда исполнителя — правило «очередь разбирает один процесс» не
      проверено — 3 раз(а)
    • отчёт об операции не доехал до владельца — 21 раз(а)
    Запись, которая не даёт сделать действие второй раз, не легла в базу …

Разбор показал три разные вещи под одним заголовком:

1. ОТЧЁТЫ ТЕРЯЛИСЬ ПО-НАСТОЯЩЕМУ. `notify_if_enabled` пробовал три раза за
   пять секунд, а дольше повторять было некому: итог операции — событие
   однократное. Теперь временный сбой кладёт сообщение в очередь доставки
   (`services/notify_outbox`), и она повторяет его до суток. Потерей
   считается только то, что не дошло и оттуда.
2. ОБМЕН ТЕМПОМ НИЧЕГО НЕ ТЕРЯЛ. На сбое исходы и состояния аккаунтов
   возвращаются в очередь и уходят следующим кругом через 5 секунд. Тревожно,
   только если база молчит долго — тогда накопленное может не дожить до неё.
3. АРЕНДА НЕ НАРУШАЛАСЬ. Держатель на сбое работает только в пределах срока
   аренды, новичок очередь не берёт. Тревожно, только если держатель отпустил
   очередь, — тогда операции стоят.

И ни 2, ни 3 — не «запись, без которой действие сделается дважды»: приписка
про двойное действие гнала владельца искать повторы, которых не было.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db
from services import metrics, notify_outbox, op_worker, pacing_store, replica_guard


def _counter(name: str) -> float:
    counters = metrics.snapshot().get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _OutboxPool:
    """Пул, у которого очередь доставки принимает строку."""

    def __init__(self, accept=True):
        self.accept = accept
        self.inserted: list[tuple] = []

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "OK"

    async def fetchval(self, query, *args):
        if "INSERT INTO notification_outbox" in query:
            if not self.accept:
                return None
            self.inserted.append(args)
            return len(self.inserted)
        return None


class _DownBot:
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("Bad Gateway")
        self.calls = 0

    async def send_message(self, *a, **kw):
        self.calls += 1
        raise self.exc


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    metrics.reset()
    db._notify_cooldown.clear()
    monkeypatch.setattr(db, "_NOTIFY_RETRY_DELAYS", (0.0, 0.0))
    yield
    db._notify_cooldown.clear()


# ── 1. Отчёт не пропадает ─────────────────────────────────────────────────


def test_a_report_telegram_did_not_take_goes_to_the_outbox_not_to_loss():
    pool = _OutboxPool()
    ok = asyncio.run(db.notify_if_enabled(
        pool, _DownBot(), 777100, "op_complete", "<b>итог</b>", dedup_key="op:1"))
    # True: доставка теперь за очередью, и отметку анти-повтора снимать нельзя
    # — иначе отчёт пришёл бы дважды.
    assert ok is True
    assert len(pool.inserted) == 1 and pool.inserted[0][0] == 777100
    assert pool.inserted[0][2] == "<b>итог</b>"
    assert _counter("infragram_notifications_undelivered_total") == 0
    assert _counter("infragram_notifications_deferred_total") == 1


def test_when_the_outbox_cannot_take_it_the_loss_is_still_counted():
    ok = asyncio.run(db.notify_if_enabled(
        _OutboxPool(accept=False), _DownBot(), 777101, "op_complete", "x"))
    assert ok is False
    assert _counter("infragram_notifications_undelivered_total") == 1


def test_a_long_telegram_limit_is_not_waited_out_inline():
    class _Limit(Exception):
        retry_after = 600

    bot = _DownBot(_Limit("Too Many Requests: retry after 600"))
    pool = _OutboxPool()
    ok = asyncio.run(asyncio.wait_for(db.notify_if_enabled(
        pool, bot, 777102, "op_complete", "x"), timeout=5))
    assert ok is True
    assert bot.calls == 1, "повтор раньше срока Telegram отбил бы снова"
    assert pool.inserted[0][5] >= 600, "очередь должна ждать срок Telegram"


def test_a_blocked_bot_does_not_fill_the_outbox():
    pool = _OutboxPool()
    asyncio.run(db.notify_if_enabled(
        pool, _DownBot(RuntimeError("Forbidden: bot was blocked by the user")),
        777103, "op_complete", "x"))
    assert pool.inserted == []


class _QueuePool:
    def __init__(self, rows):
        self.rows = rows
        self.deleted: list[int] = []
        self.rescheduled: list[tuple] = []

    async def fetch(self, query, *args):
        rows, self.rows = self.rows, []
        return rows

    async def execute(self, query, *args):
        if query.startswith("DELETE FROM notification_outbox"):
            self.deleted.append(args[0])
        elif "next_attempt_at" in query:
            self.rescheduled.append(args)
        return "OK"


def _row(rid, attempts=1, age_s=60.0):
    return {"id": rid, "user_id": 777200, "pref": "op_complete", "text": "итог",
            "reply_markup": None, "attempts": attempts, "age_s": age_s}


class _OkBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, uid, text, **kw):
        self.sent.append(text)


def test_the_outbox_delivers_and_forgets():
    pool, bot = _QueuePool([_row(1)]), _OkBot()
    assert asyncio.run(notify_outbox.deliver_once(pool, bot)) == 1
    assert pool.deleted == [1]
    assert "итог" in bot.sent[0] and "задержался" in bot.sent[0]


def test_the_outbox_backs_off_on_a_transient_failure():
    pool = _QueuePool([_row(2, attempts=3)])
    asyncio.run(notify_outbox.deliver_once(pool, _DownBot()))
    assert pool.deleted == []
    rid, wait, _err = pool.rescheduled[0]
    assert rid == 2 and wait == notify_outbox.backoff_for(4)
    assert _counter("infragram_notifications_undelivered_total") == 0


def test_the_outbox_gives_up_after_a_day_and_says_so():
    pool = _QueuePool([_row(3, attempts=20, age_s=notify_outbox._MAX_AGE_S + 1)])
    asyncio.run(notify_outbox.deliver_once(pool, _DownBot()))
    assert pool.deleted == [3]
    assert _counter("infragram_notifications_undelivered_total") == 1


# ── 2. Обмен темпом: тревога на долгий сбой, а не на блип ─────────────────


@pytest.fixture
def _pacing_reset(monkeypatch):
    monkeypatch.setattr(pacing_store, "_failing_since", None)
    monkeypatch.setattr(pacing_store, "_stall_reported", False)
    monkeypatch.setattr(pacing_store, "_cycle_failed", False)


def _cycle(failed: bool, t: float):
    if failed:
        pacing_store._metric("infragram_pacing_sync_errors_total")
    pacing_store._close_cycle(t)


def test_a_single_pacing_blip_is_not_an_alarm(_pacing_reset):
    _cycle(True, 0.0)
    _cycle(True, 5.0)
    _cycle(False, 10.0)
    assert _counter("infragram_pacing_sync_stalled_total") == 0
    assert _counter("infragram_pacing_sync_errors_total") == 2


def test_a_pacing_outage_longer_than_a_minute_is_reported_once(_pacing_reset):
    for t in range(0, 200, 5):
        _cycle(True, float(t))
    assert _counter("infragram_pacing_sync_stalled_total") == 1
    _cycle(False, 205.0)
    for t in range(210, 300, 5):
        _cycle(True, float(t))
    assert _counter("infragram_pacing_sync_stalled_total") == 2, (
        "новый эпизод после восстановления — новая тревога")


def test_the_owner_alarm_watches_the_stall_not_the_blips():
    names = op_worker._PROTECTIVE_FAILURE_COUNTERS
    assert "infragram_pacing_sync_stalled_total" in names
    assert "infragram_pacing_sync_errors_total" not in names
    assert "infragram_executor_lease_lost_total" in names
    assert "infragram_executor_lease_errors_total" not in names
    for n in ("infragram_pacing_sync_stalled_total",
              "infragram_executor_lease_lost_total",
              "infragram_notifications_deferred_total"):
        assert n in metrics._HELP, n


# ── 3. Аренда: тревога, когда очередь встала ───────────────────────────────


class _DeadPool:
    async def fetchrow(self, *a, **kw):
        raise ConnectionError("connection refused")


def test_a_lease_blip_within_its_term_is_not_a_lost_lease(monkeypatch):
    import time

    monkeypatch.setattr(replica_guard, "_lease_held_by", "me")
    monkeypatch.setattr(replica_guard, "_lease_confirmed_at", time.monotonic())
    assert asyncio.run(replica_guard.acquire_executor_lease(_DeadPool(), "me")) is True
    assert _counter("infragram_executor_lease_lost_total") == 0


def test_a_lease_that_could_not_be_renewed_in_time_is_reported(monkeypatch):
    import time

    monkeypatch.setattr(replica_guard, "_lease_held_by", "me")
    monkeypatch.setattr(replica_guard, "_lease_confirmed_at",
                        time.monotonic() - replica_guard._EXEC_LEASE_TTL_S)
    assert asyncio.run(replica_guard.acquire_executor_lease(_DeadPool(), "me")) is False
    assert _counter("infragram_executor_lease_lost_total") == 1


def test_a_newcomer_lease_blip_is_neither_taken_nor_alarmed(monkeypatch):
    monkeypatch.setattr(replica_guard, "_lease_held_by", None)
    assert asyncio.run(replica_guard.acquire_executor_lease(_DeadPool(), "me")) is False
    assert _counter("infragram_executor_lease_lost_total") == 0


# ── Текст тревоги ──────────────────────────────────────────────────────────


def test_a_database_outage_alone_does_not_claim_double_actions():
    text = op_worker._protective_alert_text([
        ("infragram_pacing_sync_stalled_total", "обмен темпом стоит", 1),
        ("infragram_executor_lease_lost_total", "аренда отпущена", 1),
    ])
    assert "База не отвечала" in text
    assert "Защита не записалась" not in text
    assert "дважды" not in text


def test_a_real_protective_write_failure_still_leads():
    text = op_worker._protective_alert_text([
        ("infragram_pacing_sync_stalled_total", "обмен темпом стоит", 1),
        ("infragram_dm_dedup_write_failures_total", "дедуп ЛС", 1),
    ])
    assert text.startswith("⚠️ <b>Защита не записалась</b>")
    assert "дважды" in text and "обмен темпом стоит — 1 раз(а)" in text
