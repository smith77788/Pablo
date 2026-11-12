"""Разведка рекламы подсказывает следующий слой рынка.

НАХОДКА. Рекламодатель-канал — сам участник рынка ниши: его рекламодатели —
ещё один круг конкурентов и площадок. Поставить его на разведку можно было
только по одному, открыв карточку рекламодателя, и экран не подсказывал, с
кого начинать: глубина разведки упиралась в то, сколько карточек владелец
откроет руками. Теперь сводка отдаёт `discover` — каналы-рекламодатели, ещё
не стоящие на разведке, по убыванию охвата ниши (в скольких каналах и как
часто рекламируются), и экран ставит их на разведку одной кнопкой.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): убрать NOT EXISTS из запроса ->
красный (уже разведанный канал снова в подсказке); убрать фильтр kind ->
красный (бот в подсказке).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_779


async def _scenario(pool):
    from services import ad_intelligence as ai

    await pool.execute("DELETE FROM ad_posts_log WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM ad_placements WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM ad_advertisers WHERE owner_id=$1", OWNER)
    for name, kind, places, chans in (
        ("big_rival", "channel", 9, 5),
        ("small_rival", "channel", 2, 1),
        ("tracked_rival", "channel", 20, 8),
        ("shop_bot", "bot", 30, 9),
        ("example.com", "site", 30, 9),
        ("legacy_rival", None, 3, 2),
    ):
        await pool.execute(
            "INSERT INTO ad_advertisers(owner_id, advertiser_username, kind, "
            "placements_count, channels_count, total_views, first_seen_at, last_seen_at) "
            "VALUES($1,$2,$3,$4,$5,0,NOW(),NOW())", OWNER, name, kind, places, chans)
    await pool.execute(
        "INSERT INTO ad_placements(owner_id, channel_username, channel_title) "
        "VALUES($1,'Tracked_Rival','T')", OWNER)

    got = [d["username"] for d in await ai.discovery_candidates(pool, OWNER)]
    assert got == ["big_rival", "legacy_rival", "small_rival"], got


def test_next_market_layer_on_live_db():
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


def test_overview_and_screen_carry_the_layer():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    api = (root / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert '"discover": discover' in api
    ui = (root / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "d.discover" in ui and "Разведать все" in ui
