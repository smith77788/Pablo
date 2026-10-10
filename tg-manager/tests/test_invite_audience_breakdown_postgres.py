"""Счётчик аудитории инвайта говорит, сколько РЕАЛЬНО пойдёт в прогон.

Дыра: «Доступно целей» считал строки источника целиком. Исполнитель же
отбрасывает уже приглашённых в этот канал, реестр «не приглашать», повторы и
фильтры аудитории. На второй кампании в тот же канал экран обещал тысячи
целей, а прогон заканчивался словами «новых нет».

Здесь — по настоящему Postgres (SQL счётчика строит ключи сам, заглушка пула их
не проверила бы) и со сверкой с ключами самого исполнителя
(`op_worker._row_to_ref` + `compare_key`). Запуск — см. docstring
tests/test_invite_e2e_postgres.py; без INFRAGRAM_TEST_DSN файл пропускается.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 990777



async def _apply_schema():
    """Настоящая схема продукта — как в tests/test_invite_e2e_postgres.py."""
    import glob
    import re

    import asyncpg

    conn = await asyncpg.connect(DSN)
    files = sorted(glob.glob("schema_v*.sql"),
                   key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
    for f in ["schema.sql"] + files:
        try:
            await conn.execute(open(f, encoding="utf-8").read())
        except Exception:
            pass  # схемы идемпотентны
    await conn.close()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _scenario():
    import asyncpg
    from services import contact_opt_out as coo
    from services import invite_dedup as idd
    from services.invite_preflight import audience_breakdown
    from services.op_worker import _row_to_ref

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await pool.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER)
        rows = [  # (username, tg_user_id, is_bot, source_id)
            ("Ivan", 1, False, 100), ("ivan", 1, False, 200),  # повтор из двух каналов
            ("Olga", 2, False, 100), (None, 555, False, 100),   # приглашена / приглашён
            ("Spam", 3, False, 100),                            # «не приглашать»
            ("Bot1", 4, True, 100),                             # бот — отсеет фильтр
            ("", 777, False, 100), ("Petr", 5, False, 100),
        ]
        for u, i, bot, src_id in rows:
            await pool.execute(
                "INSERT INTO parsed_audiences(owner_id, source_type, source_id, parse_run_id, "
                "username, tg_user_id, is_bot) VALUES($1,'channel',$5,1,$2,$3,$4)",
                OWNER, u, i, bot, src_id)
        await pool.execute(
            "INSERT INTO crm_contacts(owner_id, username, tg_user_id) VALUES"
            "($1,'Ivan',10),($1,NULL,11)", OWNER)
        if await pool.fetchval("SELECT to_regclass('invite_target_log')"):
            await pool.execute("DELETE FROM invite_target_log WHERE owner_id=$1", OWNER)
        await idd.remember(pool, OWNER, ["@chan"], ["@OLGA", "555", "11"])
        await coo.add(pool, OWNER, "@Spam")

        b = await audience_breakdown(pool, OWNER, "parsed", "@chan",
                                     aud_filters={"not_bot": True})
        b_crm = await audience_breakdown(pool, OWNER, "crm", "@chan")

        # Эталон — ключи самого исполнителя.
        opted = {coo.compare_key(t) for t in await coo.load_opted_out(pool, OWNER)}
        already = await idd.invited_keys(pool, OWNER, ["@chan"])
        src = await pool.fetch(
            "SELECT username, tg_user_id FROM parsed_audiences WHERE owner_id=$1 "
            "AND COALESCE(is_bot,FALSE)=FALSE", OWNER)
        keys, fresh = [], set()
        for r in src:
            ref, _ = _row_to_ref(r)
            if ref is None:
                continue
            k = coo.compare_key(str(ref))
            keys.append(k)
            if k not in opted and k not in already:
                fresh.add(k)
        return b, b_crm, len(keys), fresh
    finally:
        await pool.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER)
        await pool.close()


def test_breakdown_matches_what_the_executor_would_take():
    _run(_apply_schema())
    b, b_crm, n_rows, fresh = _run(_scenario())
    assert b["rows"] == n_rows == 7, "бот отсеян фильтром, как у исполнителя"
    assert b["repeats"] == 1, "'@Ivan' и '@ivan' — один человек"
    assert b["already"] == 2 and b["opted_out"] == 1
    assert b["fresh"] == len(fresh) == 3, "@ivan, 777, @petr"
    # CRM: контакт без @username приглашён по id — его не обещаем.
    assert b_crm["unique"] == 2 and b_crm["already"] == 1 and b_crm["fresh"] == 1
