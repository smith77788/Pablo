"""«Флот сейчас» на живой базе: запросы принимаются Postgres и считают верно.

Разбор текста запроса не доказывает, что база его примет (CLAUDE.md), поэтому
снимок прогоняется целиком против настоящей схемы: состав флота по статусам и
кулдауну, прогрев только по своим активным аккаунтам, операции сейчас и за
сутки, лента ограничений и счётчик разных аккаунтов с критическим
ограничением. Чужой владелец в снимок не попадает.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995871
OTHER = 995872


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_live_snapshot_counts_right_on_real_schema():
    import asyncpg
    from services import infra_memory
    from services import mini_app_dashboard as md

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=6)

        async def _clean():
            for oid in (OWNER, OTHER):
                await pool.execute("DELETE FROM restriction_events WHERE owner_id=$1", oid)
                await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", oid)
                await pool.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", oid)
                await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", oid)

        try:
            await _clean()
            infra_memory.drop_health_summary()

            async def acc(oid, phone, status, *, cooldown=False, active=True):
                return await pool.fetchval(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, cooldown_until) "
                    "VALUES($1,$2,'s',$3,$4, CASE WHEN $5 THEN now() + interval '1 hour' END) RETURNING id",
                    oid, phone, active, status, cooldown)

            a1 = await acc(OWNER, "+79990087101", "active")
            a2 = await acc(OWNER, "+79990087102", None)
            a3 = await acc(OWNER, "+79990087103", "active", cooldown=True)
            await acc(OWNER, "+79990087104", "spamblock")
            b1 = await acc(OWNER, "+79990087105", "banned")
            await acc(OWNER, "+79990087106", "session_expired")
            await acc(OWNER, "+79990087107", "active", active=False)   # выключен — не в счёт
            o1 = await acc(OTHER, "+79990087108", "active")

            for aid, oid, st in ((a1, OWNER, "active"), (a2, OWNER, "completed"), (o1, OTHER, "active")):
                await pool.execute(
                    "INSERT INTO account_warmup_plans(owner_id, account_id, status) VALUES($1,$2,$3)",
                    oid, aid, st)

            async def op(oid, status, *, finished_ago=None, done=0, total=0, label=None):
                return await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, done_items, total_items, label, "
                    " started_at, finished_at) VALUES($1,'invite',$2,$3,$4,$5, now(), "
                    " CASE WHEN $6::int IS NULL THEN NULL ELSE now() - make_interval(hours => $6::int) END) "
                    "RETURNING id", oid, status, done, total, label, finished_ago)

            run_id = await op(OWNER, "running", done=10, total=50, label="Инвайт в группу")
            await op(OWNER, "pending")
            await op(OWNER, "paused")
            await op(OWNER, "done", finished_ago=1, done=5, total=5)
            await op(OWNER, "partial", finished_ago=2, done=3, total=5)
            await op(OWNER, "failed", finished_ago=30)          # старше суток
            await op(OTHER, "running")

            for aid, et, sev in ((b1, "ban_detected", "critical"), (b1, "ban_detected", "critical"),
                                 (a3, "account_flood_risk", "warning")):
                await pool.execute(
                    "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
                    "VALUES($1,$2,$3,$4)", OWNER, aid, et, sev)
            await pool.execute(
                "INSERT INTO restriction_events(owner_id, account_id, event_type, severity) "
                "VALUES($1,$2,'ban_detected','critical')", OTHER, o1)

            async def _att(pool_, uid):
                return {"items": [], "high": 0, "total": 0}
            orig = md.attention_items
            md.attention_items = _att
            try:
                return await md.live_snapshot(pool, OWNER), run_id
            finally:
                md.attention_items = orig
        finally:
            await _clean()
            await pool.close()

    out, run_id = _run(_go())
    assert out["fleet"] == {"total": 6, "online": 2, "cooldown": 1, "restricted": 1,
                            "lost": 1, "session": 1, "warming": 1}
    assert out["pulse"] is not None and out["pulse"]["total"] == 6
    assert out["ops"] == {"running": 1, "queued": 1, "paused": 1, "done_24h": 1, "failed_24h": 1}
    assert out["active"][0]["id"] == run_id and out["active"][0]["status"] == "running"
    assert out["active"][0]["label"] == "Инвайт в группу"
    assert len(out["active"]) == 3
    titles = [e.get("title") for e in out["events"] if e["kind"] == "restriction"]
    assert titles.count("Аккаунт забанен") == 2 and "Частые флуд-ожидания" in titles
    assert sum(1 for e in out["events"] if e["kind"] == "op") == 2
    # Два события бана по ОДНОМУ аккаунту — один аккаунт; чужой не в счёт.
    assert out["critical_24h"] == 1
