"""Гейт готовности инвайтеров: неготовые аккаунты не берутся в инвайт (fail-open).

_filter_unready_for_invite тянет trust/acc_status/cooldown/proxy из БД и режет
аккаунты ниже порога readiness('invite')=0.50; если готовых нет — возвращает всех
(лучше рискнуть, чем обнулить операцию).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 994001
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
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
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


_SEQ = [0]


def _mk(pool, trust, status="active"):
    _SEQ[0] += 1
    seq = _SEQ[0]

    async def _s():
        return await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
            "trust_score,first_name) VALUES($1,$2,'sess',TRUE,$3,$4,'A') RETURNING id",
            OWNER, f"+799{seq:08d}", status, trust)
    return _run(_s())


def _clean(pool):
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))


def test_unready_accounts_filtered(pool):
    from services.op_worker import _filter_unready_for_invite
    _clean(pool)
    ready = _mk(pool, 0.9)          # высокий trust → готов
    weak = _mk(pool, 0.1)           # низкий trust → не готов
    spam = _mk(pool, 0.9, "spamblock")  # spamblock → каппится → не готов
    accounts = [{"id": ready, "session_str": "s", "proxy_url": None},
                {"id": weak, "session_str": "s", "proxy_url": None},
                {"id": spam, "session_str": "s", "proxy_url": None}]
    kept = _run(_filter_unready_for_invite(pool, 1, accounts))
    kept_ids = {a["id"] for a in kept}
    assert ready in kept_ids
    assert weak not in kept_ids
    assert spam not in kept_ids


def test_all_unready_fail_open(pool):
    from services.op_worker import _filter_unready_for_invite
    _clean(pool)
    a = _mk(pool, 0.05)
    b = _mk(pool, 0.05)
    accounts = [{"id": a, "session_str": "s", "proxy_url": None},
                {"id": b, "session_str": "s", "proxy_url": None}]
    kept = _run(_filter_unready_for_invite(pool, 1, accounts))
    # все не готовы → возвращаем всех (не обнуляем операцию)
    assert {x["id"] for x in kept} == {a, b}


def test_empty_input(pool):
    from services.op_worker import _filter_unready_for_invite
    assert _run(_filter_unready_for_invite(pool, 1, [])) == []
