"""Локальные массовые атрибуты аккаунтов (версии/пол/роль) по живому Postgres.

DB-операции без Telegram: проверяем реальные эффекты, скоуп по owner, идемпотентность
ролей и валидацию. Схема с gender-колонкой (schema_v167 + инлайн-миграция).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 993001
OTHER = 993002
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
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


def _seed(pool, owner=OWNER, n=3):
    async def _s():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
        ids = []
        for i in range(n):
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "first_name,device_model,system_version,app_version) "
                "VALUES($1,$2,$3,TRUE,'active',$4,'old','old','old') RETURNING id",
                owner, f"+79{owner%100}{i:06d}", f"s{i}", f"A{i}"))
        return ids
    return _run(_s())


def test_regenerate_versions_changes_device_fields(pool):
    from services import account_bulk_attrs as a
    ids = _seed(pool, n=3)
    n = _run(a.regenerate_versions(pool, OWNER, ids))
    assert n == 3
    rows = _run(pool.fetch(
        "SELECT device_model, system_version, app_version, lang_code FROM tg_accounts "
        "WHERE owner_id=$1", OWNER))
    # старые заглушки 'old' заменены реалистичным fingerprint'ом
    assert all(r["device_model"] != "old" and r["device_model"] for r in rows)
    assert all(r["app_version"] != "old" for r in rows)
    assert all(r["lang_code"] for r in rows)


def test_set_gender(pool):
    from services import account_bulk_attrs as a
    ids = _seed(pool, n=2)
    assert _run(a.set_gender(pool, OWNER, ids, "f")) == 2
    got = _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND gender='f'", OWNER))
    assert got == 2
    with pytest.raises(ValueError):
        _run(a.set_gender(pool, OWNER, ids, "x"))


def test_add_and_remove_role_idempotent(pool):
    from services import account_bulk_attrs as a
    ids = _seed(pool, n=2)
    assert _run(a.add_role(pool, OWNER, ids, "worker")) == 2
    # повторное добавление той же роли — no-op (0 затронуто, дублей в tags нет)
    assert _run(a.add_role(pool, OWNER, ids, "worker")) == 0
    tags = _run(pool.fetchval("SELECT tags FROM tg_accounts WHERE id=$1", ids[0]))
    assert list(tags).count("worker") == 1
    # удаление
    assert _run(a.remove_role(pool, OWNER, ids, "worker")) == 2
    assert _run(a.remove_role(pool, OWNER, ids, "worker")) == 0  # уже нет
    tags2 = _run(pool.fetchval("SELECT tags FROM tg_accounts WHERE id=$1", ids[0]))
    assert "worker" not in list(tags2 or [])


def test_scope_guard_other_owner_untouched(pool):
    from services import account_bulk_attrs as a
    mine = _seed(pool, owner=OWNER, n=2)
    theirs = _seed(pool, owner=OTHER, n=2)
    # пытаемся применить к чужим id под своим owner — не затронет
    assert _run(a.set_gender(pool, OWNER, theirs, "m")) == 0
    assert _run(a.add_role(pool, OWNER, theirs, "x")) == 0
    # чужие аккаунты без пола/роли
    g = _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND gender IS NOT NULL", OTHER))
    assert g == 0
    _ = mine
