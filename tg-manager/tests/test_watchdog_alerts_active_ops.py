"""Вотчдог застрявших операций не бьёт ложной тревогой по АКТИВНО исполняющимся
running-операциям.

Ошибку видел пользователь (скриншот): «⚠️ Застрявшие операции · #14 mass_invite —
running 49 мин (owner 8025267710)». Это была живая инвайт-операция чужого
владельца: mass_invite намеренно пейсится часами (чтобы не ловить баны) и всё
время выполняется в памяти воркера (_active_op_ids). _watchdog_stale такие НЕ
сбрасывает, а вот _watchdog_alerts помечал их «застрял» и слал админу чужой
owner_id с угрозой «running зависло → будет авто-сброшено» — хотя сброса не будет.

Тот же класс, что уже закрытый ложняк по отложенным pending. Фикс: алерт, как и
сброс, пропускает running-операции из _active_op_ids — сообщает только о реально
брошенных (сирота после падения воркера).
"""
from __future__ import annotations

import asyncio

from services import op_worker


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((int(chat_id), text))


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return list(self._rows)


def _row(op_id, status, owner_id, age_min, op_type="mass_invite"):
    return {"id": op_id, "op_type": op_type, "status": status,
            "owner_id": owner_id, "age_min": age_min}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fire(pool, monkeypatch, active_ids):
    monkeypatch.setenv("ADMIN_IDS", "424242")
    op_worker._alerted_stuck_ops.clear()
    op_worker._active_op_ids.clear()
    for i in active_ids:
        op_worker._active_op_ids.add(i)
    bot = _FakeBot()
    try:
        _run(op_worker._watchdog_alerts(pool, bot))
    finally:
        op_worker._active_op_ids.clear()
    return "\n".join(t for _, t in bot.sent)


def test_active_running_op_not_reported_stuck(monkeypatch):
    # #14 running 49 мин, но АКТИВНО исполняется (в _active_op_ids) → не застряла
    pool = _FakePool([_row(14, "running", 8025267710, 49)])
    text = _fire(pool, monkeypatch, active_ids={14})
    assert "#14" not in text, (
        "активная running-операция не застряла — ложный алерт с чужим owner_id"
    )
    assert text == "", "алертить не о чём — сообщений быть не должно"


def test_orphan_running_op_still_reported(monkeypatch):
    # такая же running-строка, но НЕ активна в памяти (сирота после падения
    # воркера) → это реально брошенная op, её вотчдог и правда сбросит → алерт
    pool = _FakePool([_row(14, "running", 8025267710, 49)])
    text = _fire(pool, monkeypatch, active_ids=set())
    assert "#14" in text, "брошенная (не активная) running-op обязана детектиться"


def test_pending_backlog_always_reported(monkeypatch):
    # pending не привязан к _active_op_ids — просроченная очередь всегда алертится
    pool = _FakePool([_row(20, "pending", 999, 30)])
    text = _fire(pool, monkeypatch, active_ids={20})   # id в active не влияет на pending
    assert "#20" in text


def test_mixed_only_active_running_filtered(monkeypatch):
    pool = _FakePool([
        _row(14, "running", 111, 49),   # активна → скрыть
        _row(15, "running", 222, 70),   # сирота → показать
        _row(20, "pending", 333, 30),   # backlog → показать
    ])
    text = _fire(pool, monkeypatch, active_ids={14})
    assert "#14" not in text
    assert "#15" in text and "#20" in text
