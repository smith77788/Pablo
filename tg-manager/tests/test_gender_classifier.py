"""Классификатор пола по имени: юнит-контракт + разметка базы на живом Postgres."""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

from services import gender_classifier as gc


# ── юнит: детерминированный контракт classify() (без БД) ─────────────────────

class TestClassify:
    def test_known_male(self):
        for n in ("Иван", "Пётр", "Александр", "Сергей", "Дмитрий", "Никита", "Илья"):
            assert gc.classify(n) == "m", n

    def test_known_female(self):
        for n in ("Анна", "Мария", "Ольга", "Екатерина", "Наталья", "Любовь", "Юлия"):
            assert gc.classify(n) == "f", n

    def test_unisex_diminutive_is_unknown(self):
        # Саша/Женя — и муж, и жен: без доп. сигнала не гадаем
        assert gc.classify("Саша") is None
        assert gc.classify("Женя") is None

    def test_ending_heuristic_for_unknown_names(self):
        assert gc.classify("Милослава") == "f"   # -а
        assert gc.classify("Радислав") == "m"     # согласная
        assert gc.classify("Мокий") == "m"        # -й

    def test_soft_sign_ambiguous_is_none_unless_known(self):
        # неизвестное имя на -ь → None (Игорь/Любовь известны и разрешаются словарём)
        assert gc.classify("Ксмынь") is None
        assert gc.classify("Игорь") == "m"
        assert gc.classify("Любовь") == "f"

    def test_empty_and_junk(self):
        assert gc.classify("") is None
        assert gc.classify(None) is None
        assert gc.classify("12345") is None

    def test_full_name_uses_first_token(self):
        assert gc.classify("Владимир Петров") == "m"
        assert gc.classify("ольга") == "f"  # регистронезависимо


# ── e2e: разметка parsed_audiences.gender на живом Postgres ──────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 991301
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _seed_run(pool, names):
    async def _s():
        await pool.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM parser_runs WHERE owner_id=$1", OWNER)
        run_id = await pool.fetchval(
            "INSERT INTO parser_runs(owner_id,source_type,source_ref,parse_type,status) "
            "VALUES($1,'channel','X','members','done') RETURNING id", OWNER)
        for i, nm in enumerate(names):
            await pool.execute(
                "INSERT INTO parsed_audiences(owner_id,source_type,source_id,parse_run_id,"
                "tg_user_id,first_name) VALUES($1,'channel',$2,$2,$3,$4)",
                OWNER, run_id, 1000 + i, nm)
        return run_id
    return _run(_s())


def test_classify_audience_writes_gender_and_breakdown(pool):
    run_id = _seed_run(pool, ["Иван", "Анна", "Мария", "Пётр", "Саша", "12345"])
    res = _run(gc.classify_audience(pool, OWNER, run_id))
    assert res["total"] == 6
    assert res["m"] == 2   # Иван, Пётр
    assert res["f"] == 2   # Анна, Мария
    assert res["unknown"] == 2  # Саша (унисекс), 12345 (мусор)
    # gender реально записан в БД
    m = _run(pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 AND gender='m'", OWNER))
    f = _run(pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 AND gender='f'", OWNER))
    assert (m, f) == (2, 2)


def test_classify_audience_is_idempotent(pool):
    run_id = _seed_run(pool, ["Ольга", "Сергей"])
    r1 = _run(gc.classify_audience(pool, OWNER, run_id))
    r2 = _run(gc.classify_audience(pool, OWNER, run_id))
    assert r1 == r2
