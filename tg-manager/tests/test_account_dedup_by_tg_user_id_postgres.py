"""Повторное добавление уже подключённого аккаунта НЕ создаёт дубликат.

ЧТО ЛОМАЛОСЬ. Дедуп аккаунтов шёл только по ключу (owner_id, phone). Но телефон
— ненадёжный идентификатор: при импорте из tdata/.session Telegram часто отдаёт
ПУСТОЙ номер, и он подставляется как "id:<uid>", тогда как при входе по номеру
стоит реальный "+7…". Один и тот же аккаунт, добавленный двумя способами (или
один и тот же tdata, залитый дважды), получал РАЗНЫЙ phone → ON CONFLICT не
срабатывал → в базе появлялся ВТОРОЙ аккаунт той же личности Telegram. Для
владельца это «добавил случайно второй раз — и теперь дубль во флоте».

ЧТО ТЕПЕРЬ. add_tg_account дедуплицирует по tg_user_id (неизменная личность):
если аккаунт с этим tg_user_id у владельца уже есть, phone приводится к
существующему, и ON CONFLICT ОБНОВЛЯЕТ ту же строку (освежает сессию), а не
плодит дубликат.

Проверка на живом Postgres — заглушка пула не знает про UNIQUE(owner_id, phone)
и про SELECT-дедуп, то есть на юнит-тестах баг невидим в принципе.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 880077
UID = 555123


async def _load_schema(conn) -> None:
    for f in ["schema.sql"] + sorted(
        glob.glob("schema_v*.sql"),
        key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)),
    ):
        try:
            await conn.execute(open(f, encoding="utf-8").read())
        except Exception:
            pass


def test_reimport_same_account_different_phone_no_duplicate(monkeypatch):
    import asyncio

    import asyncpg

    from database import db

    # Изолируем дедуп от побочных эффектов (шифрование/журнал/контакт-синк).
    import services.token_vault as tv
    import services.contacts_hub.device_sync as ds

    monkeypatch.setattr(tv, "encrypt_token", lambda s: s)
    monkeypatch.setattr(db, "record_phone_link", _anoop)
    monkeypatch.setattr(ds, "schedule_for_new_account", _anoop)

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

            # 1) Подключили по номеру — реальный телефон.
            id1 = await db.add_tg_account(
                pool, owner_id=OWNER, phone="+79990000001", session_str="SESS_1",
                tg_user_id=UID, first_name="Акк", username="acc")

            # 2) Тот же аккаунт из tdata — Telegram спрятал номер → "id:<uid>".
            id2 = await db.add_tg_account(
                pool, owner_id=OWNER, phone=f"id:{UID}", session_str="SESS_2",
                tg_user_id=UID, first_name="Акк", username="acc")

            cnt = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND tg_user_id=$2",
                OWNER, UID)
            assert id1 == id2, "переподключение завело НОВЫЙ id вместо обновления"
            assert cnt == 1, f"дубликат аккаунта: {cnt} строк на один tg_user_id"

            # Сессия обновилась на существующей строке (а не осталась старой).
            enc = await pool.fetchval(
                "SELECT session_str FROM tg_accounts WHERE id=$1", id1)
            assert enc == "SESS_2", "сессия не обновилась при переподключении"

            # Телефон сохранён исходный — это стабильная подпись и ключ уникальности.
            ph = await pool.fetchval("SELECT phone FROM tg_accounts WHERE id=$1", id1)
            assert ph == "+79990000001"
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()

    asyncio.run(go())


def test_same_phone_reimport_still_updates_in_place(monkeypatch):
    """Регресс-страховка: обычный переимпорт по тому же телефону по-прежнему
    обновляет строку (поведение ON CONFLICT (owner_id, phone) не сломано)."""
    import asyncio

    import asyncpg

    from database import db
    import services.token_vault as tv
    import services.contacts_hub.device_sync as ds

    monkeypatch.setattr(tv, "encrypt_token", lambda s: s)
    monkeypatch.setattr(db, "record_phone_link", _anoop)
    monkeypatch.setattr(ds, "schedule_for_new_account", _anoop)

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            id1 = await db.add_tg_account(
                pool, owner_id=OWNER, phone="+79990000002", session_str="A",
                tg_user_id=UID + 1, first_name="B", username="b")
            id2 = await db.add_tg_account(
                pool, owner_id=OWNER, phone="+79990000002", session_str="B",
                tg_user_id=UID + 1, first_name="B", username="b")
            cnt = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", OWNER)
            assert id1 == id2 and cnt == 1
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()

    asyncio.run(go())


async def _anoop(*a, **k):
    return None
