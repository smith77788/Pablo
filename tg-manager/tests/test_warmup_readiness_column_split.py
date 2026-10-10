"""Регресс: warmup_level и readiness_level делили одну колонку и затирали
друг друга — Invite Advisor падал 500 на любом реально прогретом аккаунте.

Первопричина. `services/account_warmer.py` пишет "light"/"medium"/"deep" в
`tg_accounts.warmup_level` после каждой сессии прогрева, и СРАЗУ следующей
строкой вызывает `account_readiness.refresh_account_readiness()`, которая
писала СВОЙ, несовместимый словарь ("blocked"/"raw"/"warming"/"ready"/
"veteran") в ТУ ЖЕ колонку — затирая то, что account_warmer.py только что
записал. `services/invite_advisor.py` и `services/flood_engine.py` читали
эту колонку через `int(warmup_level or 0)`, что падало ValueError на любой
из этих строк (кроме NULL).

Фикс (schema_v201): readiness получает свою колонку `readiness_level`,
warmup_level остаётся только за account_warmer.py; оба читателя переведены
на строковую проверку «пусто = ни разу не прогревался».
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991401

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p_: int(re.search(r"schema_v(\d+)", p_).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    yield


async def _mk_account(pool, **over) -> int:
    row = await pool.fetchrow(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, is_active, "
        "trust_score, warmup_level) "
        "VALUES($1,$2,'s','active',TRUE,$3,$4) RETURNING id",
        OWNER, over.get("phone", "+79990000001"),
        over.get("trust_score", 0.5), over.get("warmup_level"),
    )
    return int(row["id"])


def test_refresh_account_readiness_does_not_touch_warmup_level(pool):
    """Ядро бага: запись readiness больше не задевает чужую колонку."""
    from services import account_readiness

    acc_id = _run(_mk_account(pool, warmup_level="deep"))

    _run(account_readiness.refresh_account_readiness(pool, acc_id, OWNER))

    row = _run(pool.fetchrow(
        "SELECT warmup_level, readiness_level FROM tg_accounts WHERE id=$1", acc_id))
    assert row["warmup_level"] == "deep", (
        "readiness не имеет права затирать warmup_level account_warmer.py"
    )
    assert row["readiness_level"] is not None, "readiness обязана попасть в СВОЮ колонку"


def test_readiness_level_gets_a_real_value_from_the_vocabulary(pool):
    from services import account_readiness

    acc_id = _run(_mk_account(pool, warmup_level=None))
    _run(account_readiness.refresh_account_readiness(pool, acc_id, OWNER))

    row = _run(pool.fetchrow("SELECT readiness_level FROM tg_accounts WHERE id=$1", acc_id))
    assert row["readiness_level"] in ("blocked", "raw", "warming", "ready", "veteran")


def test_warmed_accounts_survive_a_readiness_refresh_and_stay_readable(pool):
    """Сквозной сценарий бага: прогрели (warmup_level=light/medium/deep) →
    прошла реклассификация готовности → invite_advisor всё ещё может это
    прочитать без падения."""
    from services import account_readiness, invite_advisor

    ids = [_run(_mk_account(pool, phone=f"+7999000{i:04d}", warmup_level=lvl))
           for i, lvl in enumerate(["light", "medium", "deep"])]
    for acc_id in ids:
        _run(account_readiness.refresh_account_readiness(pool, acc_id, OWNER))

    # warmup_level обязан остаться нетронутым после readiness-прохода...
    rows = _run(pool.fetch(
        "SELECT warmup_level FROM tg_accounts WHERE id = ANY($1::bigint[]) ORDER BY id", ids))
    assert {r["warmup_level"] for r in rows} == {"light", "medium", "deep"}

    # ...и invite_advisor не должен падать на этих значениях (регресс ValueError).
    res = _run(invite_advisor.build_advice(pool, OWNER))
    assert isinstance(res["advice"], list)
