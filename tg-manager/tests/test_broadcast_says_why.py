"""Рассылка говорит владельцу, почему она ждёт, остановилась или кому не дошла.

ЧТО БЫЛО. «Ошибка» и «0 / 1 000» приходили без объяснения: отозванный токен,
заблокировавшая бота аудитория, ошибка в разметке и Telegram, который не
ответил (рассылка ждёт докатки), выглядели одинаково, а разбивка причин жила
только в логе сервера. Теперь причина пишется в broadcasts.status_note.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.test_broadcast_survives_telegram_outage_on_preflight import (
    BAD_GW, _run_broadcast,
)


def test_note_names_the_reasons_in_russian():
    from services.broadcaster import status_note

    note = status_note("partial", 7, {"blocked": 5, "bad_request": 2})
    assert note == "Не доставлено: заблокировали бота — 5, ошибка в тексте или кнопках — 2"
    assert status_note("done", 0, {}) is None
    assert status_note("failed", 0, {}) == "Не отправлено ни одного сообщения"
    assert status_note("cancelled", 0, {}, "остановлена владельцем") == "Остановлена владельцем"
    assert status_note("partial", 3, {"unauthorized": 3}, "токен бота отозван") == (
        "Остановлена: токен бота отозван; Не доставлено: токен бота отозван — 3")


def _notes(monkeypatch, replies):
    from database import db

    notes: list = []

    async def _note(pool, bid, note):
        notes.append(note)

    monkeypatch.setattr(db, "set_broadcast_note", _note)
    _run_broadcast(monkeypatch, replies)
    return notes


def test_waiting_for_telegram_is_explained(monkeypatch):
    from services import broadcaster
    assert _notes(monkeypatch, [BAD_GW] * 10) == [broadcaster.NOTE_TELEGRAM_DOWN]


def test_revoked_token_is_explained(monkeypatch):
    from services import broadcaster
    notes = _notes(monkeypatch, [{"ok": False, "error_code": 401,
                                  "description": "Unauthorized"}] * 2)
    assert notes == [broadcaster.NOTE_TOKEN_REVOKED]


def test_note_column_exists_postgres():
    dsn = os.getenv("INFRAGRAM_TEST_DSN", "")
    if not dsn:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from database import db

    async def go():
        try:
            conn = await asyncpg.connect(dsn)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            tx = conn.transaction()
            await tx.start()
            await conn.execute(
                "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
                "VALUES('880011001:t', 880011001, 'note_test_bot', TRUE, 1)")
            bid = await conn.fetchval(
                "INSERT INTO broadcasts(bot_id, message_text, status, created_by) "
                "VALUES(880011001, 'привет', 'running', 1) RETURNING id")
            await db.set_broadcast_note(conn, bid, "Не доставлено: заблокировали бота — 1")
            assert await conn.fetchval(
                "SELECT status_note FROM broadcasts WHERE id=$1", bid) == (
                "Не доставлено: заблокировали бота — 1")
            await db.set_broadcast_note(conn, bid, None)
            assert await conn.fetchval(
                "SELECT status_note FROM broadcasts WHERE id=$1", bid) is None
            await tx.rollback()
        finally:
            await conn.close()

    asyncio.run(go())
