"""Стадия 2 #4: durable-гидратация flood-state после рестарта + typing-статус.

Part A (hydrate_states) — на живом Postgres (нужны реальные bigint[]/EXTRACT/интервалы;
заглушка пула типы не проверяет). Part B (typing_duration/проводка) — чисто, без сети.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Part B: typing (чистое) ──────────────────────────────────────────────────

def test_typing_duration_bounds_and_monotonic():
    from services.account_console import typing_duration
    for n in (0, 1, 50, 500, 5000):
        for _ in range(50):
            d = typing_duration(n)
            assert 0.6 <= d <= 8.0, (n, d)   # clamp жёсткий: ни мгновенно, ни вечность
    # длиннее текст → в среднем дольше набор
    short = sum(typing_duration(5) for _ in range(300)) / 300
    long = sum(typing_duration(400) for _ in range(300)) / 300
    assert long > short


def test_typing_wired_before_send():
    src = open(os.path.join(ROOT, "services/account_console.py"), encoding="utf-8").read()
    seg = src[src.index("async def send_text"):src.index("async def send_file")]
    assert "_simulate_typing(client, entity, text)" in seg, "нет статуса «печатает…» перед отправкой"
    # именно ПЕРЕД отправкой
    assert seg.index("_simulate_typing") < seg.index("send_message"), "typing должен идти до send_message"


def test_hydrate_wired_at_op_start():
    ow = open(os.path.join(ROOT, "services/op_worker.py"), encoding="utf-8").read()
    assert "hydrate_states(pool" in ow, "flood-state не гидрируется на старте операции"


# ── Part A: hydrate_states на живом Postgres ─────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pgmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres (INFRAGRAM_TEST_DSN)")

OWNER = 996001
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(glob.glob(os.path.join(ROOT, "schema_v*.sql")),
                                         key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(os.path.join(ROOT, os.path.basename(f)), encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=3)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pgmark
def test_hydrate_restores_cooldown_and_flood_count(pool):
    """После «рестарта» (пустой in-memory) durable cooldown/флуды поднимаются из БД."""
    from services import flood_engine as fe

    async def _seed():
        await pool.execute("DELETE FROM account_flood_log WHERE account_id IN "
                           "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        acc = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, "
            "cooldown_until) VALUES($1,'+79960000001','s',TRUE,'cooldown', NOW()+INTERVAL '600 seconds') "
            "RETURNING id", OWNER)
        for _ in range(3):
            await pool.execute(
                "INSERT INTO account_flood_log(account_id, flood_seconds, action_type) "
                "VALUES($1, 60, 'invite')", acc)
        return acc

    acc_id = _run(_seed())
    # Симулируем рестарт: чистим in-memory состояние движка.
    fe._flood_state.pop(acc_id, None)
    fe._hydrated.discard(acc_id)

    n = _run(fe.hydrate_states(pool, [acc_id]))
    assert n == 1
    # cooldown восстановлен → аккаунт числится «остывающим», а не свободным
    assert fe.is_account_cooling(acc_id) is True, "durable cooldown должен пережить рестарт"
    st = fe.get_account_state(acc_id)
    assert st.total_floods_24h >= 3 and st.risk_score > 0
    # идемпотентность: повторный вызов не гидрирует снова
    assert _run(fe.hydrate_states(pool, [acc_id])) == 0
