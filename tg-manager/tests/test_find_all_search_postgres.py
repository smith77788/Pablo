"""Поиск по своему хозяйству на живой базе: находки, группы, чужой владелец.

Шесть запросов по шести таблицам с ILIKE, ESCAPE, COALESCE и джойном —
заглушка пула подтвердила бы форму кода, а не то, что Postgres их примет и
отберёт правильные строки. Ровно этот класс ломался молча (CLAUDE.md:
«проверка текста запроса не проверяет, что база его примет»).

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/ci pytest …
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 991561, 991562
BOT_ID, OTHER_BOT = 880099561, 880099562
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    ids = [OWNER, STRANGER]
    await pool.execute("DELETE FROM broadcasts WHERE bot_id = ANY($1::bigint[])",
                       [BOT_ID, OTHER_BOT])
    await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                       [BOT_ID, OTHER_BOT])
    for sql in (
        "DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])",
        "DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])",
        "DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
        "DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
    ):
        await pool.execute(sql, ids)


async def _seed(pool):
    await _clean(pool)
    # Слово «вега» спрятано в каждой из шести таблиц — по разным полям.
    await pool.execute(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, first_name) "
        "VALUES($1,'+79990000561','s',TRUE,'Вега Иванова')", OWNER)
    await pool.execute(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, first_name) "
        "VALUES($1,'+79990000562','s',TRUE,'Вега Чужая')", STRANGER)
    await pool.execute(
        "INSERT INTO managed_bots(token, bot_id, username, first_name, is_active, added_by) "
        "VALUES($1,$2,'vega_shop_bot','Вега магазин',TRUE,$3)",
        f"1:find-{BOT_ID}", BOT_ID, OWNER)
    await pool.execute(
        "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
        "VALUES($1,$2,'vega_other_bot',TRUE,$3)", f"1:find-{OTHER_BOT}", OTHER_BOT, STRANGER)
    acc = await pool.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
        "VALUES($1,'+79990000563','s',TRUE) RETURNING id", OWNER)
    await pool.execute(
        "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, type, members_count) "
        "VALUES($1,$2,-1001561,'Канал Вега','megagroup',10)", OWNER, acc)
    await pool.execute(
        "INSERT INTO unified_contacts(owner_id, first_name, last_name, company) "
        "VALUES($1,'Пётр','Вегин','Вега ООО')", OWNER)
    await pool.execute(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
        "VALUES($1,'Скидка от Вега на всё',5,'completed',$2)", BOT_ID, OWNER)
    await pool.execute(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,'mass_check','done','{}'::jsonb,1,'Прогрев Вега')", OWNER)
    # Ловушка на джокеры: строка с процентом не должна находиться по «_».
    await pool.execute(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, first_name) "
        "VALUES($1,'+79990000564','s',TRUE,'Скидка 100%')", OWNER)


@live
def test_one_query_finds_every_kind_and_only_mine():
    from services import mini_app_global_search as G

    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await _seed(pool)
            return (await G.search_all(pool, OWNER, "вега"),
                    await G.search_all(pool, STRANGER, "вега"),
                    await G.search_all(pool, OWNER, "Вег_"),
                    await G.search_all(pool, OWNER, "100%"))
        finally:
            try:
                await _clean(pool)
            finally:
                await pool.close()

    mine, stranger, underscore, percent = _run(go())
    assert not mine["failed"], mine["failed"]
    kinds = {g["key"]: g["items"] for g in mine["groups"]}
    assert set(kinds) == {"accounts", "bots", "channels", "contacts",
                          "broadcasts", "operations"}, sorted(kinds)
    assert kinds["accounts"][0]["title"] == "Вега Иванова"
    assert kinds["bots"][0]["title"] == "@vega_shop_bot"
    assert kinds["channels"][0]["title"] == "Канал Вега"
    assert kinds["contacts"][0]["title"] == "Пётр Вегин"
    assert kinds["operations"][0]["title"] == "Прогрев Вега"
    # У рассылки своего экрана нет: находка несёт ЗАПРОС, а не id строки.
    assert kinds["broadcasts"][0]["fn"] == "openBroadcastSearch"
    assert kinds["broadcasts"][0]["arg"] == "вега"

    # Чужое хозяйство в мои находки не попадает, и наоборот.
    assert all("Чужая" not in i["title"]
               for g in mine["groups"] for i in g["items"]), mine
    s_kinds = {g["key"]: g["items"] for g in stranger["groups"]}
    assert s_kinds["accounts"][0]["title"] == "Вега Чужая"
    assert "channels" not in s_kinds and "operations" not in s_kinds

    # Джокеры ILIKE — это символы, а не «что угодно».
    # «Вег_» — это «Вег» и подчёркивание, а не «Вег» и любой символ: иначе
    # запрос нашёл бы всю «Вегу» и поиск врал бы молча.
    assert underscore["found"] == 0, (
        "«Вег_» нашёл "
        f"{[i['title'] for g in underscore['groups'] for i in g['items']]} — "
        "значит подчёркивание ушло в базу джокером")
    assert [i["title"] for g in percent["groups"] for i in g["items"]] == ["Скидка 100%"]
