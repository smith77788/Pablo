"""Единый запуск кампании веером ставит дочерние операции под общим id.

Это суть модуля: один спек (каналы + чаты + боты + ДМ) → одна кампания, которая
раздаёт работу существующим движкам и связывает их дочерние операции вместе, чтобы
один экран показывал весь прогресс. Проверяем на живом Postgres: группа создана,
на каждый ресурс ушла дочерняя операция с тегом campaign_id, для ДМ заведена строка
dm_campaigns, а сводка видит все ветки. operation_bus.submit подменён лёгкой
вставкой — тип операции/тариф тестируются отдельно, здесь важна именно раскладка.
"""
from __future__ import annotations

import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 990088


async def _load_schema(conn) -> None:
    for f in ["schema.sql"] + sorted(
        glob.glob("schema_v*.sql"),
        key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)),
    ):
        try:
            await conn.execute(open(f, encoding="utf-8").read())
        except Exception:
            pass


def _jsonb(v):
    return json.loads(v) if isinstance(v, str) else v


def test_launch_fans_out_and_groups(monkeypatch):
    import asyncio

    import asyncpg

    from services import campaign_hub as ch
    from services import operation_bus as obus

    async def _fake_submit(pool, owner_id, op_type, params, *, total_items=0,
                           scheduled_for=None, template_id=None, max_retries=None,
                           label=None, bypass_plan_check=False, dedup_window_sec=0):
        return await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,$2,'pending',$3::jsonb,$4,$5) RETURNING id",
            owner_id, op_type, json.dumps(params), int(total_items or 0), label or "")

    monkeypatch.setattr(obus, "submit", _fake_submit)

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM broadcast_campaigns WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)

            spec = {
                "name": "Промо",
                "content": {"text": "Привет"},
                "settings": {"silent": True, "delay_seconds": 4},
                "targets": {
                    "channels": {"enabled": True, "channel_ids": [111, 222]},
                    "chats": {"enabled": True, "acc_ids": [1, 2]},
                    "bots": {"enabled": True, "segment": "all_each"},
                    "dm": {"enabled": True, "target_type": "all_bots"},
                },
            }
            res = await ch.launch_campaign(pool, OWNER, spec)
            assert res["status"] == "running", res
            kinds = [c["kind"] for c in res["children"]]
            assert kinds.count("channels") == 1
            assert kinds.count("chats") == 2   # по одной операции на аккаунт
            assert kinds.count("bots") == 1
            assert kinds.count("dm") == 1

            cid = res["campaign_id"]
            n = await pool.fetchval(
                "SELECT COUNT(*) FROM broadcast_campaign_ops WHERE campaign_id=$1", cid)
            assert n == 5

            # Каждая дочерняя операция помечена общим campaign_id.
            tagged = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue q "
                "JOIN broadcast_campaign_ops o ON o.op_id=q.id "
                "WHERE o.campaign_id=$1 AND (q.params->>'broadcast_campaign_id')=$2",
                cid, str(cid))
            assert tagged == 5

            # ДМ-ветка завела строку dm_campaigns, и dm_campaign ссылается на неё.
            dm_child = next(c for c in res["children"] if c["kind"] == "dm")
            dm_params = _jsonb(await pool.fetchval(
                "SELECT params FROM operation_queue WHERE id=$1", dm_child["op_id"]))
            dm_row_id = int(dm_params["campaign_id"])
            dmrow = await pool.fetchrow(
                "SELECT owner_id, status, target_type FROM dm_campaigns WHERE id=$1", dm_row_id)
            assert dmrow["owner_id"] == OWNER
            assert dmrow["status"] == "running"
            assert dmrow["target_type"] == "all_bots"

            # Общие настройки доехали до дочерних (silent/delay у каналов).
            chan_child = next(c for c in res["children"] if c["kind"] == "channels")
            chan_params = _jsonb(await pool.fetchval(
                "SELECT params FROM operation_queue WHERE id=$1", chan_child["op_id"]))
            assert chan_params["silent"] is True and chan_params["delay_seconds"] == 4

            # Единая сводка видит все четыре ветки.
            st = await ch.campaign_status(pool, OWNER, cid)
            assert st["totals"]["ops"] == 5
            assert set(st["kinds"]) == {"channels", "chats", "bots", "dm"}

            # Чужой владелец кампанию не видит.
            assert await ch.campaign_status(pool, OWNER + 1, cid) is None

            await pool.execute("DELETE FROM broadcast_campaigns WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
        finally:
            await pool.close()

    asyncio.run(go())


def test_invalid_spec_rejected_before_group_created(monkeypatch):
    import asyncio

    import asyncpg

    from services import campaign_hub as ch

    async def go():
        conn = await asyncpg.connect(DSN)
        await _load_schema(conn)
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM broadcast_campaigns WHERE owner_id=$1", OWNER)
            before = await pool.fetchval(
                "SELECT COUNT(*) FROM broadcast_campaigns WHERE owner_id=$1", OWNER)
            with pytest.raises(ch.CampaignSpecError):
                await ch.launch_campaign(pool, OWNER, {"content": {"text": ""}, "targets": {}})
            after = await pool.fetchval(
                "SELECT COUNT(*) FROM broadcast_campaigns WHERE owner_id=$1", OWNER)
            assert before == after, "негодный спек не должен создавать строку кампании"
        finally:
            await pool.close()

    asyncio.run(go())
