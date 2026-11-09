"""Администратор видит, что срывается чаще всего и у скольких людей.

ЧТО БЫЛО. Текст ошибки каждой сорвавшейся операции лежал в базе, но увидеть
его можно было только по одной операции. Какой сбой самый частый и скольких
пользователей он задел — ответить было нечем, чинили по громкости жалоб.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import error_digest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
needs_db = pytest.mark.skipif(not DSN, reason="нужна живая база INFRAGRAM_TEST_DSN")

OWNERS = (990_072_001, 990_072_002, 990_072_003)


def test_the_same_trouble_with_different_names_is_one_group():
    a = error_digest.normalize_error("Канал @alpha_news не найден (id 123456)")
    b = error_digest.normalize_error("Канал @beta_shop не найден (id 98)")
    assert a == b
    assert error_digest.normalize_error("FloodWait 37 с\nTraceback…") == \
        error_digest.normalize_error("FloodWait 1200 с")
    assert error_digest.normalize_error("") == "без текста ошибки"
    assert error_digest.normalize_error("таймаут") != error_digest.normalize_error("нет прав")


def test_what_hit_more_people_comes_first():
    rows = [{"op_type": "bulk_join", "error": "нет аккаунтов", "owner_id": 1,
             "id": i, "finished_at": None} for i in range(50)]
    rows += [{"op_type": "mass_publish", "error": f"канал @c{i} не найден",
              "owner_id": 10 + i, "id": 100 + i, "finished_at": None} for i in range(3)]
    top = error_digest.group_failures(rows)
    assert [(g["op_type"], g["users"], g["count"]) for g in top] == [
        ("mass_publish", 3, 3), ("bulk_join", 1, 50)]


@needs_db
def test_the_digest_counts_failed_operations_from_the_queue():
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute(
                "DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])", list(OWNERS))
            for i, owner in enumerate(OWNERS):
                await pool.execute(
                    """INSERT INTO operation_queue(owner_id, op_type, status, error_msg, finished_at)
                       VALUES($1, 'digest_probe', 'failed', $2, now())""",
                    owner, f"Канал @probe{i} не найден")
            await pool.execute(
                """INSERT INTO operation_queue(owner_id, op_type, status, last_error, finished_at)
                   VALUES($1, 'digest_probe', 'failed', 'старый сбой', now() - INTERVAL '30 days')""",
                OWNERS[0])
            await pool.execute(
                """INSERT INTO operation_queue(owner_id, op_type, status, finished_at)
                   VALUES($1, 'digest_probe', 'done', now())""", OWNERS[0])
            return await error_digest.top_failures(pool, days=7, limit=500)
        finally:
            await pool.execute(
                "DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])", list(OWNERS))
            await pool.close()

    d = asyncio.run(go())
    mine = [g for g in d["groups"] if g["op_type"] == "digest_probe"]
    assert len(mine) == 1, mine
    assert (mine[0]["count"], mine[0]["users"]) == (3, 3)
    assert "не найден" in mine[0]["error"] and "probe" not in mine[0]["error"]
    assert d["failed"] >= 3 and d["finished"] >= d["failed"] + 1


def test_the_admin_screen_names_the_operation_and_the_share():
    from bot.handlers import admin
    text = admin._failures_text({
        "days": 7, "finished": 200, "failed": 10, "users_hit": 4,
        "groups": [{"op_type": "bulk_join", "error": "нет <аккаунтов>", "count": 6,
                    "users": 3, "last_at": None, "example_id": 77, "example": ""}]})
    assert "10" in text and "5.0%" in text and "людей: 3" in text and "№77" in text
    assert "&lt;аккаунтов&gt;" in text, "текст ошибки должен быть экранирован"
    empty = admin._failures_text({"days": 1, "finished": 5, "failed": 0,
                                  "users_hit": 0, "groups": []})
    assert "ни одна операция не сорвалась" in empty
