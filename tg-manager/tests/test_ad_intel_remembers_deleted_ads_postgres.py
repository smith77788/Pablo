"""Разведка рекламы помнит удалённую рекламу и видит повторные закупки.

НАХОДКА. Повторный скан удалял из журнала ВСЕ рекламные посты, которых не
было в свежей выдаче. Так уходили не только переразмеченные (что правильно),
но и удалённые каналом — а удаление через сутки и есть формат размещения
«1/24». Журнал терял ровно такие размещения, счётчики рекламодателя падали.
Теперь пост остаётся с отметкой `deleted_at`, карточка канала знает формат
(«1/24», «2/48», «навсегда») и долю повторных закупок.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): вернуть прежний DELETE по `msg_id >= min`
-> красный test_deleted_ad_stays_with_a_mark_postgres.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

import pytest

from services import ad_intelligence as ai

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_782


def test_signals_from_numbers():
    s = ai.placement_signals([3, 1, 2, 1], ads_total=7, ads_deleted=6, median_life_h=25.0)
    assert s["repeat_advertisers"] == 2 and s["repeat_share"] == 0.5
    assert s["ad_format"] == "1/24"
    assert ai.placement_signals([1], 5, 0, None)["ad_format"] == "навсегда"
    assert ai.placement_signals([1], 5, 4, 47.0)["ad_format"] == "2/48"
    assert ai.placement_signals([], 1, 1, 20.0)["ad_format"] == ""


def _msg(mid, text, hours_ago):
    return SimpleNamespace(
        id=mid, message=text, text=text, views=1000, forwards=0, replies=None,
        reactions=None, entities=None, reply_markup=None, fwd_from=None,
        date=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago))


class _Client:
    def __init__(self, msgs):
        self.msgs = msgs

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_entity(self, ref):
        return SimpleNamespace(id=777, title="Канал", participants_count=5000)

    async def iter_messages(self, entity, limit=100):
        for m in self.msgs:
            yield m


def test_deleted_ad_stays_with_a_mark_postgres(monkeypatch):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from services import account_manager

    ad = "Реклама. Подписывайтесь: t.me/rival_channel erid: 2SDnjc1234"
    first = [_msg(110, "обычный пост", 1), _msg(109, ad, 30), _msg(108, "пост", 40),
             _msg(107, ad, 50), _msg(106, "пост", 60)]
    # Второй скан: рекламу 109 канал удалил, 107 — отстояла.
    second = [m for m in first if m.id != 109]
    holder = {"msgs": first}
    monkeypatch.setattr(account_manager, "_make_client",
                        lambda s, a=None: _Client(holder["msgs"]))

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM ad_posts_log WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM ad_placements WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM ad_advertisers WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
                "VALUES($1,'+79990001782','s',TRUE,'active') RETURNING id", OWNER)
            r1 = await ai.scan_channel_ads(pool, "watched_chan", acc, OWNER)
            assert r1["status"] == "ok" and r1["ad_posts_found"] == 2, r1
            holder["msgs"] = second
            r2 = await ai.scan_channel_ads(pool, "watched_chan", acc, OWNER)
            assert r2["status"] == "ok", r2
            rows = {r["msg_id"]: r["deleted_at"] for r in await pool.fetch(
                "SELECT msg_id, deleted_at FROM ad_posts_log WHERE owner_id=$1", OWNER)}
            assert set(rows) == {107, 109}, (
                f"удалённая каналом реклама пропала из журнала: {rows}")
            assert rows[109] is not None and rows[107] is None
            adv = await pool.fetchrow(
                "SELECT placements_count FROM ad_advertisers WHERE owner_id=$1 "
                "AND advertiser_username='rival_channel'", OWNER)
            assert adv and adv["placements_count"] == 2
            card = await ai.channel_detail(pool, OWNER, "watched_chan")
            assert card["ads_deleted"] == 1
            assert card["repeat_advertisers"] == 1 and card["repeat_share"] == 1.0
            assert card["advertisers"][0]["repeat"] is True
        finally:
            await pool.close()

    asyncio.run(_go())
