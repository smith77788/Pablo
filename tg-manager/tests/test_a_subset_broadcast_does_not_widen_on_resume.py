"""Рассылка по подмножеству не расширяется до всей аудитории после рестарта.

После рестарта процесса `broadcaster.resume_interrupted` докатывает рассылки
в статусах ('pending','running') и берёт список получателей из самой строки:
`target_user_ids` — список (подмножество) или NULL (вся аудитория бота). То
есть не сохранённое подмножество означает «догонять по всем».

Строку рассылки заранее создают ровно те пути, где аудитория задана списком:
«повтор недоставленным» (`broadcaster.resend_undelivered`) и варианты
A/B-теста. Исполнитель сохранял список только для СЕГМЕНТНОЙ рассылки
(`elif _seg_sql`), поэтому их список не сохранялся никогда — и рестарт посреди
отправки (а он бывает на каждом деплое) догонял рассылку по всей аудитории:
люди, уже получившие исходное сообщение, получали его второй раз.

Ровно от этого `resend_undelivered` отказывается, когда у исходной рассылки
вычищен журнал доставки: «сообщение ушло бы всем подписчикам заново».
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services import broadcaster, op_worker


class _FakePool:
    def __init__(self, bc_status="done", sent=3):
        self.bc = {"status": bc_status, "sent_count": sent, "failed_count": 0}
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        if "FROM managed_bots" in query:
            return {"token": "1:AA", "bot_id": 555}
        if "FROM broadcasts" in query:
            return dict(self.bc)
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetch(self, query, *args):
        if "FROM bot_users" in query:
            return [{"user_id": i} for i in range(1, 51)]   # вся аудитория — 50
        return []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    def wrote(self, needle):
        return [a for q, a in self.executed if needle in q]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(op_worker, "_BROADCAST_POLL_S", 0, raising=False)
    monkeypatch.setattr(broadcaster, "start", lambda *a, **kw: None)
    monkeypatch.setattr(broadcaster, "is_running", lambda bid: False)
    monkeypatch.setattr(broadcaster, "cancel", lambda bid: True)
    op_worker._cancel_cache.clear()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec(pool, params):
    return _run(op_worker._exec_run_broadcast(pool, None, 42, 777, dict(params)))


def test_an_explicit_recipient_list_is_stored_on_the_broadcast_row():
    """Повтор недоставленным: строка рассылки создана заранее, список — в params."""
    pool = _FakePool()
    undelivered = [11, 12, 13]
    _exec(pool, {"bot_id": 555, "broadcast_id": 900, "text": "повтор",
                 "user_ids": undelivered})
    saved = pool.wrote("SET target_user_ids=")
    assert saved, (
        "список недоставленных не сохранён в строке рассылки: рестарт посреди "
        "отправки догонит её по ВСЕЙ аудитории бота, и те, кто уже получил "
        "исходное сообщение, получат его второй раз")
    assert json.loads(saved[-1][0]) == undelivered, saved[-1]


def test_a_segment_list_is_still_stored():
    """Контроль: сегментная рассылка как сохранялась, так и сохраняется."""
    pool = _FakePool()
    _exec(pool, {"bot_id": 555, "broadcast_id": 900, "text": "сегмент",
                 "segment": "active_7d"})
    saved = pool.wrote("SET target_user_ids=")
    assert saved, "сегментный список перестал сохраняться"
    assert len(json.loads(saved[-1][0])) == 50


def test_a_full_audience_broadcast_keeps_the_list_null():
    """Обратная сторона: полной аудитории список сохранять НЕЛЬЗЯ.

    Сохранённый список заморозил бы состав подписчиков на момент запуска — те,
    кто подписался после, не получат рассылку никогда.
    """
    pool = _FakePool()
    _exec(pool, {"bot_id": 555, "broadcast_id": 900, "text": "всем"})
    assert not pool.wrote("SET target_user_ids="), (
        "полная аудитория заморожена списком на момент запуска")
