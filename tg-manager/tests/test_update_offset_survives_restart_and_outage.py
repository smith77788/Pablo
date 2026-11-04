"""Оффсет обновлений бота не теряет и не повторяет сообщения.

ЧТО БЫЛО.
* `_init_offset` (первый запуск бота или сброс «начать с новых») на неуспехе
  getUpdates — сеть, 5xx, флуд — считал очередь пустой и ставил метку 1.
  Следующий опрос с offset=2 приносил весь накопленный за сутки хвост, и бот
  отвечал на вчерашние сообщения, которые должен был пропустить.
* `set_update_offset` писал значение как есть: опрос, закончивший пакет
  позже вебхука (смена режима доставки), откатывал оффсет назад, и повтор
  Telegram разбирался второй раз — дважды авто-ответ, дважды шаг сценария.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
BOT_ID = 880077991



def test_init_offset_does_not_mark_the_queue_empty_on_outage(monkeypatch):
    from services import auto_responder as ar

    saved: list[int] = []

    async def _call(http, token, method, **kw):
        assert method == "getUpdates"
        return {"ok": False, "error_code": 502, "description": "Bad Gateway"}

    async def _set(pool, bot_id, off):
        saved.append(off)

    monkeypatch.setattr(ar.bot_api, "_call", _call)
    monkeypatch.setattr(ar.db, "set_update_offset", _set)
    assert asyncio.run(ar._init_offset(None, None, BOT_ID, "1:x")) == 0
    assert saved == [], f"сбой Telegram записан как «очередь пуста»: {saved}"


def test_init_offset_skips_the_backlog_when_telegram_answers(monkeypatch):
    from services import auto_responder as ar

    saved: list[int] = []

    async def _call(http, token, method, **kw):
        return {"ok": True, "result": [{"update_id": 555}]}

    async def _set(pool, bot_id, off):
        saved.append(off)

    monkeypatch.setattr(ar.bot_api, "_call", _call)
    monkeypatch.setattr(ar.db, "set_update_offset", _set)
    assert asyncio.run(ar._init_offset(None, None, BOT_ID, "1:x")) == 555
    assert saved == [555]


def test_offset_never_moves_back_but_can_be_reset():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from database import db

    async def go():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await p.execute("DELETE FROM bot_update_offsets WHERE bot_id=$1", BOT_ID)
            await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
            await p.execute(
                "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
                "VALUES($1,$2,'offset_test_bot',TRUE,993774)", f"{BOT_ID}:t", BOT_ID)
            await db.set_update_offset(p, BOT_ID, 111)   # вебхук разобрал 111
            await db.set_update_offset(p, BOT_ID, 110)   # опрос закончил пакет позже
            assert await db.get_update_offset(p, BOT_ID) == 111, "оффсет откатился назад"
            await db.set_update_offset(p, BOT_ID, 0)     # «начать с новых»
            assert await db.get_update_offset(p, BOT_ID) == 0
        finally:
            await p.execute("DELETE FROM bot_update_offsets WHERE bot_id=$1", BOT_ID)
            await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
            await p.close()

    asyncio.run(go())
