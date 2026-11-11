"""Разведка рекламы на живой базе: повторный скан не дублирует, счётчики
рекламодателя пересчитываются, а не растут, удаление канала их чинит.

Прежний код на каждом скане заново вставлял те же посты в ad_posts_log и
прибавлял рекламодателю +1 размещение — три скана одного канала давали
утроенные цифры на экране. Проверка запросов по тексту этого не видит: нужна
настоящая база (уникальный индекс, ON CONFLICT, RETURNING xmax).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_777


async def _scenario(pool):
    from services import ad_intelligence as ai

    async with pool.acquire() as c:
        await c.execute("DELETE FROM ad_posts_log WHERE owner_id=$1", OWNER)
        await c.execute("DELETE FROM ad_placements WHERE owner_id=$1", OWNER)
        await c.execute("DELETE FROM ad_advertisers WHERE owner_id=$1", OWNER)

    m = ai.channel_metrics([ai.PostStat(views=500, reactions=10)], 1000)
    sig = ai.analyze_ad_post("Реклама. Бот: t.me/shop_bot", own_username="chan_a")
    assert sig.is_ad and sig.advertiser == "shop_bot"
    when = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    async def scan_once(channel):
        async with pool.acquire() as conn, conn.transaction():
            pid = await ai._upsert_placement(
                conn, OWNER, channel, channel.upper(), 1000, m, 55.0, 500,
                ["IT"], 10, when)
            touched = await ai._advertisers_of_placement(conn, pid)
            new = 0
            for mid in (101, 102):
                new += await ai._save_ad_post(conn, pid, OWNER, mid, when, sig,
                                              "Реклама", 400, 0.02)
                touched.add(sig.advertiser)
            await ai.refresh_advertisers(conn, OWNER, touched, "IT")
            return new

    assert await scan_once("chan_a") == 2
    assert await scan_once("chan_a") == 0, "повторный скан вставил те же посты ещё раз"
    await scan_once("chan_b")

    row = await pool.fetchrow(
        "SELECT placements_count, channels_count, total_views, kind FROM ad_advertisers "
        "WHERE owner_id=$1 AND advertiser_username='shop_bot'", OWNER)
    assert row["placements_count"] == 4, "счётчик растёт на каждом скане, а не считает посты"
    assert row["channels_count"] == 2 and row["total_views"] == 1600
    assert row["kind"] == "bot"

    lst = await ai.list_channels(pool, OWNER, q="chan", sort="price", niche="IT")
    assert lst["total"] == 2 and lst["niches"][0]["niche"] == "IT"
    assert (await ai.list_advertisers(pool, OWNER, kind="bot"))["total"] == 1
    ads = await ai.list_ads(pool, OWNER, days=3650)
    assert ads["total"] == 4 and ads["items"][0]["advertiser"] == "shop_bot"
    card = await ai.channel_detail(pool, OWNER, "t.me/chan_a")
    assert card and len(card["ads"]) == 2 and card["advertisers"][0]["posts"] == 2
    adv = await ai.advertiser_detail(pool, OWNER, "@shop_bot")
    assert adv and len(adv["channels_list"]) == 2 and adv["can_track"] is False

    assert await ai.set_auto_rescan(pool, OWNER, "chan_b", False)
    await pool.execute("UPDATE ad_placements SET last_scanned_at = NOW() - INTERVAL '2 days' "
                       "WHERE owner_id=$1", OWNER)
    due = [d["channel"] for d in await ai.channels_due_for_rescan(pool, limit=10_000)
           if d["owner_id"] == OWNER]
    assert due == ["chan_a"], "канал с выключенным автообновлением попал в очередь"

    assert await ai.delete_channel(pool, OWNER, "chan_a")
    row = await pool.fetchrow(
        "SELECT placements_count, channels_count FROM ad_advertisers "
        "WHERE owner_id=$1 AND advertiser_username='shop_bot'", OWNER)
    assert (row["placements_count"], row["channels_count"]) == (2, 1)
    assert await ai.delete_channel(pool, OWNER, "chan_b")
    assert not await pool.fetchval(
        "SELECT 1 FROM ad_advertisers WHERE owner_id=$1", OWNER), \
        "рекламодатель без единого поста остался в списке"

    ov = await pool.fetchval("SELECT COUNT(*) FROM ad_posts_log WHERE owner_id=$1", OWNER)
    assert ov == 0


def test_ad_intel_rescan_and_counters_on_live_db():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await _scenario(pool)
        finally:
            await pool.close()
    asyncio.run(_go())
