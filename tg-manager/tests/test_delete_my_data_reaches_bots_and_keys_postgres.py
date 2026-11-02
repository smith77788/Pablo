"""«Удалить мои данные» удаляет ботов, их аудиторию и API-ключи — на живой базе.

Первая версия `owner_data` искала данные владельца ТОЛЬКО по колонке owner_id.
А у части таблиц владелец записан иначе, и как раз у самых важных:

* `managed_bots.added_by` — боты с токенами. Пока бот оставался, не срабатывал
  и каскад `ON DELETE CASCADE` на его аудиторию (`bot_users`), рассылки и
  отложенные рассылки: подписчики — персональные данные третьих лиц, а
  отложенная рассылка продолжала бы уходить людям;
* `api_keys.user_id` — личный ключ REST API продолжал пускать в API;
* у таблиц бота без внешнего ключа (`bot_user_facts`) владельца нет вовсе —
  их находит только bot_id ботов владельца.

Владелец при этом читал «✅ Ваши данные стёрты». Выгрузка «Мои данные» теряла
те же таблицы.

Схема ниже — урезанная копия настоящей (те же колонки, ключи и каскады), чтобы
проверка шла быстро и не зависела от полного baseline.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

ME, OTHER = 7412345678, 555
MY_BOT, OTHER_BOT = 7012345678, 6012345678


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _db(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(DSN)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


_SCHEMA = [
    """CREATE TABLE platform_users(user_id bigint PRIMARY KEY, username text,
           first_name text, notes text)""",
    "CREATE TABLE payments(id serial, user_id bigint, amount int)",
    """CREATE TABLE managed_bots(id serial PRIMARY KEY, token text NOT NULL UNIQUE,
           bot_id bigint NOT NULL UNIQUE, username text, added_by bigint NOT NULL)""",
    """CREATE TABLE bot_users(bot_id bigint NOT NULL REFERENCES managed_bots(bot_id)
           ON DELETE CASCADE, user_id bigint NOT NULL, first_name text)""",
    """CREATE TABLE scheduled_broadcasts(id serial, bot_id bigint NOT NULL
           REFERENCES managed_bots(bot_id) ON DELETE CASCADE, message_text text,
           created_by bigint NOT NULL)""",
    "CREATE TABLE bot_user_facts(bot_id bigint, user_id bigint, fact_key text, fact_value text)",
    """CREATE TABLE api_keys(id serial, user_id bigint NOT NULL, key_hash text,
           is_active boolean DEFAULT true)""",
    "CREATE TABLE tg_accounts(id serial, owner_id bigint, session_str text)",
]


async def _setup():
    import asyncpg
    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute("DROP DATABASE IF EXISTS owner_data_bots WITH (FORCE)")
        await admin.execute("CREATE DATABASE owner_data_bots")
    finally:
        await admin.close()
    pool = await asyncpg.create_pool(_db("owner_data_bots"), min_size=1, max_size=2)
    async with pool.acquire() as c:
        for sql in _SCHEMA:
            await c.execute(sql)
        await c.execute("INSERT INTO platform_users VALUES ($1,'me','Я',null),($2,'x','Y',null)",
                        ME, OTHER)
        await c.execute("INSERT INTO payments(user_id, amount) VALUES ($1, 10)", ME)
        await c.execute("INSERT INTO managed_bots(token, bot_id, username, added_by) "
                        "VALUES ('ENC:t1', $1, 'mybot', $2), ('ENC:t2', $3, 'other', $4)",
                        MY_BOT, ME, OTHER_BOT, OTHER)
        await c.execute("INSERT INTO bot_users VALUES ($1, 1, 'Подписчик'), ($2, 2, 'Чужой')",
                        MY_BOT, OTHER_BOT)
        await c.execute("INSERT INTO scheduled_broadcasts(bot_id, message_text, created_by) "
                        "VALUES ($1, 'завтра', $2), ($3, 'чужая', $4)",
                        MY_BOT, ME, OTHER_BOT, OTHER)
        await c.execute("INSERT INTO bot_user_facts VALUES ($1, 1, 'город', 'Казань'), "
                        "($2, 2, 'город', 'Сочи')", MY_BOT, OTHER_BOT)
        await c.execute("INSERT INTO api_keys(user_id, key_hash) VALUES ($1, 'h1'), ($2, 'h2')",
                        ME, OTHER)
        await c.execute("INSERT INTO tg_accounts(owner_id, session_str) VALUES ($1, 'ENC:s')", ME)
    return pool


async def _teardown(pool):
    import asyncpg
    await pool.close()
    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute("DROP DATABASE IF EXISTS owner_data_bots WITH (FORCE)")
    finally:
        await admin.close()


def test_delete_reaches_bots_audience_and_api_keys():
    from services import owner_data

    async def go():
        pool = await _setup()
        try:
            res = await owner_data.delete_owner_data(pool, ME)
            async with pool.acquire() as c:
                left = {
                    "managed_bots": await c.fetchval(
                        "SELECT count(*) FROM managed_bots WHERE added_by=$1", ME),
                    "bot_users": await c.fetchval(
                        "SELECT count(*) FROM bot_users WHERE bot_id=$1", MY_BOT),
                    "scheduled_broadcasts": await c.fetchval(
                        "SELECT count(*) FROM scheduled_broadcasts WHERE created_by=$1", ME),
                    "bot_user_facts": await c.fetchval(
                        "SELECT count(*) FROM bot_user_facts WHERE bot_id=$1", MY_BOT),
                    "api_keys": await c.fetchval(
                        "SELECT count(*) FROM api_keys WHERE user_id=$1", ME),
                    "tg_accounts": await c.fetchval(
                        "SELECT count(*) FROM tg_accounts WHERE owner_id=$1", ME),
                }
                assert all(v == 0 for v in left.values()), (
                    f"после «данные стёрты» осталось: {left}; удалено: {res}")
                # Чужое не тронуто, учёт оплат сохранён.
                assert await c.fetchval("SELECT count(*) FROM managed_bots WHERE added_by=$1",
                                        OTHER) == 1
                assert await c.fetchval("SELECT count(*) FROM bot_users WHERE bot_id=$1",
                                        OTHER_BOT) == 1
                assert await c.fetchval("SELECT count(*) FROM bot_user_facts WHERE bot_id=$1",
                                        OTHER_BOT) == 1
                assert await c.fetchval("SELECT count(*) FROM api_keys WHERE user_id=$1",
                                        OTHER) == 1
                assert await c.fetchval("SELECT count(*) FROM payments WHERE user_id=$1",
                                        ME) == 1
        finally:
            await _teardown(pool)

    _run(go())


def test_export_includes_bots_and_their_audience():
    from services import owner_data

    async def go():
        pool = await _setup()
        try:
            blob, counts = await owner_data.export_owner_data(pool, ME)
            assert counts.get("managed_bots") == 1 and counts.get("bot_users") == 1, counts
            assert counts.get("api_keys") == 1, counts
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                bots = json.loads(zf.read("managed_bots.json"))
                assert bots[0]["token"] == "<зашифровано>"
                assert "Чужой" not in zf.read("bot_users.json").decode()
                assert "Сочи" not in zf.read("bot_user_facts.json").decode()
        finally:
            await _teardown(pool)

    _run(go())
