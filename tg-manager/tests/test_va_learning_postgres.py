"""Learning SQL, one-time evidence and rollback against a PostgreSQL-compatible server."""
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services import va_learning as learning

DSN = os.getenv("TEST_VA_LEARNING_DSN") or os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна тестовая БД PostgreSQL")
ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def database():
    import asyncpg

    schema = "va_learning_test_" + uuid.uuid4().hex
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        for name in ("schema_v222_va_channel_brain.sql", "schema_v223_va_channel_posts.sql",
                     "schema_v228_va_channel_admin.sql", "schema_v236_va_learning_samples.sql"):
            await conn.execute((ROOT / name).read_text(encoding="utf-8"))
        # The new migration must also be safe to apply twice.
        await conn.execute((ROOT / "schema_v236_va_learning_samples.sql").read_text(encoding="utf-8"))
    finally:
        await conn.close()
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=int(os.getenv("VA_LEARNING_TEST_POOL_SIZE", "2")),
        server_settings={"search_path": schema},
    )
    try:
        await pool.execute("INSERT INTO va_channel_admin(owner_id,channel_id) VALUES(7,8)")
        await pool.execute(
            "INSERT INTO va_channel_brain(owner_id,channel_key,pillars,mix_weights,brand_rules) "
            "VALUES(7,'8',$1::jsonb,$2::jsonb,$3::jsonb)",
            json.dumps(["a", "b"]), json.dumps({"a": 3, "b": 3}), json.dumps({"max_chars": 900}),
        )
        yield pool
    finally:
        await pool.close()
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn.close()


async def add_sample(pool, pillar, views, *, age=25, owner=7, channel="8"):
    now = datetime.now(UTC)
    post_id = await pool.fetchval(
        "INSERT INTO va_channel_posts(owner_id,channel_key,pillar,published_at) "
        "VALUES($1,$2,$3,$4) RETURNING id", owner, channel, pillar, now - timedelta(hours=age),
    )
    await learning.capture_sample(pool, owner, int(channel), post_id,
                                  {"views": views, "reactions": 0, "forwards": 0}, now)
    return post_id


async def add_batch(pool):
    return [await add_sample(pool, p, score) for p, score in [("a", 100)] * 3 + [("b", 500)] * 3]


@pytest.mark.asyncio
async def test_measurement_age_immutability_and_owner_scope():
    async with database() as pool:
        for age, captured in [(1, False), (24, True), (25, True), (30, True), (31, False), (100, False)]:
            post_id = await add_sample(pool, "a", 100, age=age)
            row = await pool.fetchrow("SELECT * FROM va_channel_posts WHERE id=$1", post_id)
            assert (row["learning_sampled_at"] is not None) is captured
        post_id = await add_sample(pool, "a", 100)
        stats = {"views": 999, "reactions": 99, "forwards": 99}
        await learning.capture_sample(pool, 7, 8, post_id, stats, datetime.now(UTC))
        assert await pool.fetchval("SELECT learning_views FROM va_channel_posts WHERE id=$1", post_id) == 100
        untouched = await pool.fetchval(
            "INSERT INTO va_channel_posts(owner_id,channel_key,pillar,published_at) "
            "VALUES(70,'80','a',now()-interval '25 hours') RETURNING id")
        await learning.capture_sample(pool, 7, 8, untouched, stats, datetime.now(UTC))
        assert await pool.fetchval("SELECT learning_views FROM va_channel_posts WHERE id=$1", untouched) is None


@pytest.mark.asyncio
async def test_concurrent_learning_consumes_posts_once_and_keeps_owner_rules():
    async with database() as pool:
        await add_batch(pool)
        await add_sample(pool, "a", 100000, owner=70)
        await add_sample(pool, "a", 100000, channel="80")
        results = await asyncio.gather(learning.autotune(pool, 7, 8), learning.autotune(pool, 7, 8))
        assert sum(result is not None for result in results) == 1
        assert {"a": 2, "b": 4} in results
        assert await learning.autotune(pool, 7, 8) is None
        assert await pool.fetchval("SELECT count(*) FROM va_channel_posts WHERE learned_at IS NOT NULL") == 6
        assert await pool.fetchval("SELECT count(*) FROM va_admin_events WHERE kind='tune'") == 1
        row = await pool.fetchrow("SELECT brand_rules, mix_weights FROM va_channel_brain")
        assert json.loads(row["brand_rules"]) == {"max_chars": 900}
        assert json.loads(row["mix_weights"]) == {"a": 2, "b": 4}
        await add_batch(pool)
        assert await learning.autotune(pool, 7, 8) == {"a": 1, "b": 5}


@pytest.mark.asyncio
async def test_event_failure_rolls_back_policy_and_consumed_samples():
    import asyncpg

    async with database() as pool:
        await add_batch(pool)
        await pool.execute("""
            CREATE FUNCTION reject_learning_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test event failure'; END $$;
            CREATE TRIGGER reject_learning_event BEFORE INSERT ON va_admin_events
            FOR EACH ROW EXECUTE FUNCTION reject_learning_event();
        """)
        with pytest.raises(asyncpg.PostgresError, match="test event failure"):
            await learning.autotune(pool, 7, 8)
        weights = await pool.fetchval("SELECT mix_weights FROM va_channel_brain")
        assert json.loads(weights) == {"a": 3, "b": 3}
        assert await pool.fetchval("SELECT count(*) FROM va_channel_posts WHERE learned_at IS NOT NULL") == 0
        await pool.execute("DROP TRIGGER reject_learning_event ON va_admin_events")
        assert await learning.autotune(pool, 7, 8) == {"a": 2, "b": 4}


@pytest.mark.asyncio
async def test_disabled_or_insufficient_data_does_not_consume_evidence():
    async with database() as pool:
        await add_batch(pool)
        await pool.execute("UPDATE va_channel_admin SET auto_tune=FALSE")
        assert await learning.autotune(pool, 7, 8) is None
        assert await pool.fetchval("SELECT count(*) FROM va_channel_posts WHERE learned_at IS NOT NULL") == 0
        await pool.execute("UPDATE va_channel_admin SET auto_tune=TRUE")
        await pool.execute("UPDATE va_channel_posts SET pillar='removed' WHERE pillar='b'")
        assert await learning.autotune(pool, 7, 8) is None
        assert await pool.fetchval("SELECT count(*) FROM va_admin_events") == 0
