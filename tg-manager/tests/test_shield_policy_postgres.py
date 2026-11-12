"""Щит и модель риска на ЖИВОЙ базе: запросы, которые Postgres обязан принять.

Разбор текста запроса не доказывает, что база его примет (см. CLAUDE.md про
`$1::bigint[]` против uuid). Здесь — настоящий проход:

* `physics_engine.compute_owner` считает риск по журналам и пишет factors;
  у аккаунта со старой высокой оценкой и без операций она остывает;
* пауза щита ставит срок, а `resume_expired` по истечении возвращает аккаунт,
  но не мёртвый;
* `run_owner` проходит флот целиком, а исключённый аккаунт не трогает.

Запуск: INFRAGRAM_TEST_DSN=postgresql://postgres@127.0.0.1:5433/<база>.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_317


def _go(body):
    """Пул живёт внутри одного цикла событий вместе с телом теста."""
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _main():
        try:
            p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        except Exception as exc:
            pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
        try:
            await _clean(p)
            await body(p)
        finally:
            await _clean(p)
            await p.close()

    asyncio.run(_main())


async def _clean(p):
    await p.execute("DELETE FROM shield_actions WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER)
    await p.execute(
        "DELETE FROM account_risk_scores WHERE account_id IN "
        "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)


async def _acc(p, phone, **kw):
    cols = {"owner_id": OWNER, "phone": phone, "session_str": "s-" + phone,
            "is_active": True, **kw}
    names = ", ".join(cols)
    ph = ", ".join(f"${i + 1}" for i in range(len(cols)))
    return await p.fetchval(
        f"INSERT INTO tg_accounts ({names}) VALUES ({ph}) RETURNING id", *cols.values())


def test_risk_is_computed_from_the_real_journals():
    from services import physics_engine as pe

    async def body(pool):
        clean = await _acc(pool, "+7001")
        flooded = await _acc(pool, "+7002")
        stale = await _acc(pool, "+7003")
        for _ in range(3):
            await pool.execute(
                "INSERT INTO account_flood_log (account_id, operation, flood_seconds) "
                "VALUES ($1, 'invite', 1800)", flooded)
        # Старая высокая оценка без единой операции — раньше она не остывала.
        await pool.execute(
            "INSERT INTO account_risk_scores (account_id, risk_score, ban_probability) "
            "VALUES ($1, 0.95, 0.9)", stale)
        n = await pe.compute_owner(pool, OWNER)
        assert n == 3
        rows = {r["account_id"]: r for r in await pool.fetch(
            "SELECT * FROM account_risk_scores WHERE account_id = ANY($1::int[])",
            [clean, flooded, stale])}
        assert rows[stale]["risk_score"] < 0.5, "старая оценка не остыла"
        assert rows[flooded]["risk_score"] > rows[clean]["risk_score"]
        f = pe._parse_factors(rows[flooded]["factors"])
        assert {"floods_24h", "floods_7d", "long_flood"} <= {x["code"] for x in f}
        risk = await pe.get_account_risk(pool, flooded)
        assert risk["factors"], "factors не читаются обратно"

    _go(body)


def test_shield_pause_ends_by_itself():
    from services import account_shield as sh

    async def body(pool):
        acc = await _acc(pool, "+7011")
        dead = await _acc(pool, "+7012")
        cfg = sh.ShieldConfig(cool_duration_hours=1)
        for a in (acc, dead):
            d = sh.ShieldDecision(account_id=a, risk_score=0.95,
                                  ban_probability=0.9, action="pause",
                                  note="Пауза на 1 ч: проверка")
            assert await sh.apply_shield_decision(pool, d, OWNER, cfg=cfg)
        row = await pool.fetchrow(
            "SELECT is_active, shield_paused_until FROM tg_accounts WHERE id=$1", acc)
        assert row["is_active"] is False and row["shield_paused_until"] is not None
        # Срок прошёл; второй аккаунт за время паузы получил спам-блок.
        await pool.execute(
            "UPDATE tg_accounts SET shield_paused_until = NOW() - INTERVAL '1 minute' "
            "WHERE owner_id=$1", OWNER)
        await pool.execute("UPDATE tg_accounts SET acc_status='spamblock' WHERE id=$1", dead)
        back = await sh.resume_expired(pool, OWNER)
        assert back == [acc]
        row = await pool.fetchrow(
            "SELECT is_active, shield_paused_until, status_reason FROM tg_accounts WHERE id=$1", acc)
        assert row["is_active"] is True and row["shield_paused_until"] is None
        assert row["status_reason"] is None
        drow = await pool.fetchrow(
            "SELECT is_active, shield_paused_until FROM tg_accounts WHERE id=$1", dead)
        assert drow["is_active"] is False and drow["shield_paused_until"] is None
        hist = await pool.fetch(
            "SELECT action FROM shield_actions WHERE owner_id=$1 AND account_id=$2",
            OWNER, acc)
        assert "resume" in {h["action"] for h in hist}

    _go(body)


def test_manual_resume_and_full_pass():
    from services import account_shield as sh

    async def body(pool):
        risky = await _acc(pool, "+7021", acc_status="active")
        spared = await _acc(pool, "+7022")
        for a in (risky, spared):
            await pool.execute(
                "INSERT INTO restriction_events (owner_id, account_id, event_type, severity) "
                "VALUES ($1, $2, 'account_restricted', 'critical')", OWNER, a)
        await sh.save_shield_config(pool, OWNER, preset="careful",
                                    exempt_account_ids=[spared])
        cfg = await sh.get_shield_config(pool, OWNER)
        assert cfg.preset == "careful" and cfg.exempt_account_ids == [spared]
        res = await sh.run_owner(pool, OWNER, None)
        assert res["exempt"] == 1
        assert res["paused"] == 1, res
        row = await pool.fetchrow("SELECT is_active FROM tg_accounts WHERE id=$1", spared)
        assert row["is_active"] is True, "исключённый аккаунт тронут"
        assert await sh.resume_now(pool, OWNER, risky)
        assert not await sh.resume_now(pool, OWNER + 1, risky), "чужой аккаунт вернулся"
        row = await pool.fetchrow(
            "SELECT is_active, shield_paused_until FROM tg_accounts WHERE id=$1", risky)
        assert row["is_active"] is True and row["shield_paused_until"] is None
        # Повтор прохода сразу же не пишет в историю ту же запись ещё раз.
        before = await pool.fetchval("SELECT COUNT(*) FROM shield_actions WHERE owner_id=$1", OWNER)
        await pool.execute(
            "UPDATE shield_configs SET response_mode='warn' WHERE owner_id=$1", OWNER)
        await sh.run_owner(pool, OWNER, None)
        mid = await pool.fetchval("SELECT COUNT(*) FROM shield_actions WHERE owner_id=$1", OWNER)
        await sh.run_owner(pool, OWNER, None)
        after = await pool.fetchval("SELECT COUNT(*) FROM shield_actions WHERE owner_id=$1", OWNER)
        assert mid == before + 1 and after == mid, (before, mid, after)

    _go(body)
