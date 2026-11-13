"""Очистка использует результаты владельца и существующие таблицы ресурсов."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg
import pytest
from database import db
from services import restriction_cleanup as cleanup

DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой PostgreSQL")
OWNER = 992658
BOT = 992659
CHANNEL = 9007199254740993
BOT_TARGET = {"kind": "bot", "id": str(BOT)}
CHANNEL_TARGET = {"kind": "channel", "id": str(CHANNEL)}


async def _clear(pool):
    await pool.execute(
        "DELETE FROM network_edges WHERE instance_id IN "
        "(SELECT id FROM network_instances WHERE owner_id=$1)", OWNER)
    for table, column in (
        ("network_instances", "owner_id"), ("workflow_definitions", "owner_id"),
        ("operation_audit", "owner_id"), ("operation_queue", "owner_id"),
        ("managed_channels", "owner_id"), ("removed_channels", "owner_id"),
        ("va_channel_admin", "owner_id"),
        ("managed_bots", "added_by"),
    ):
        await pool.execute(f"DELETE FROM {table} WHERE {column}=$1", OWNER)


async def _run(scenario):
    async with asyncpg.create_pool(DSN, min_size=1, max_size=4) as pool:
        await pool.execute(Path("schema_v371_restriction_cleanup.sql").read_text(encoding="utf-8"))
        await _clear(pool)
        try:
            await pool.execute(
                "INSERT INTO managed_bots (bot_id, added_by, token, first_name) "
                "VALUES($1,$2,$3,'Бот')", BOT, OWNER, "12345:restriction-cleanup-test")
            await pool.execute(
                "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, type) "
                "VALUES($1,111,$2,'Канал','channel')", OWNER, CHANNEL)
            report = {"findings_version": 2, "findings": [
                {**BOT_TARGET, "status": "restricted"},
                {**CHANNEL_TARGET, "status": "check_failed"},
                {"kind": "channel", "id": "99", "status": "restricted"},
            ]}
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, result) "
                "VALUES($1,'check_owned_restrictions','done',$2::jsonb) RETURNING id",
                OWNER, json.dumps(report))
            await scenario(pool, op_id)
        finally:
            await _clear(pool)


def test_owner_exact_ids_preview_and_reversible_cleanup(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-restriction-cleanup")

    async def scenario(pool, op_id):
        result = await cleanup.findings(pool, OWNER, op_id)
        assert [item["id"] for item in result["findings"]] == [str(BOT), str(CHANNEL)]
        assert result["findings"][1]["status"] == "check_failed"
        assert not result["legacy"]
        with pytest.raises(cleanup.CleanupError) as other:
            await cleanup.findings(pool, OWNER + 1, op_id)
        assert other.value.status == 404
        await pool.execute(
            "INSERT INTO va_channel_admin(owner_id,channel_id,enabled) VALUES($1,$2,TRUE)",
            OWNER, CHANNEL)
        targets = [BOT_TARGET, CHANNEL_TARGET]
        planned = await cleanup.plan(pool, OWNER, op_id, targets, "disable")

        def verify(current):
            assert current == planned

        changed = await cleanup.apply(pool, OWNER, op_id, targets, "disable", verify=verify)
        assert changed["count"] == 2
        assert await db.add_bot(pool, "12345:restriction-cleanup-test", BOT, "user", "Бот", OWNER) is False
        assert not await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
        assert (await db.set_managed_bot_active(pool, BOT, OWNER, True))["is_active"]
        assert await db.add_managed_channels(pool, OWNER, 111, [{"id": CHANNEL}]) == 0
        assert not await pool.fetchval(
            "SELECT enabled FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2",
            OWNER, CHANNEL)
        assert await db.restore_removed_channel(pool, OWNER, CHANNEL)
        assert await pool.fetchval(
            "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2",
            OWNER, op_id) == 2

    asyncio.run(_run(scenario))


def test_reject_changed_edges_and_active_workflow_before_mutation():
    async def scenario(pool, op_id):
        project_id = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Проект') RETURNING id", OWNER)
        channel_node = await pool.fetchval(
            "INSERT INTO network_nodes(instance_id,node_type,ref_id) "
            "VALUES($1,'channel',$2) RETURNING id", project_id, CHANNEL)
        bot_node = await pool.fetchval(
            "INSERT INTO network_nodes(instance_id,node_type,ref_id) "
            "VALUES($1,'bot',$2) RETURNING id", project_id, BOT)
        targets = [BOT_TARGET, CHANNEL_TARGET]
        stale = await cleanup.plan(pool, OWNER, op_id, targets, "detach", project_id=project_id)
        edge_id = await pool.fetchval(
            "INSERT INTO network_edges(instance_id,source_node_id,target_node_id) "
            "VALUES($1,$2,$3) RETURNING id", project_id, channel_node, bot_node)

        def verify_stale(current):
            if current != stale:
                raise cleanup.CleanupError("План изменился", 409)

        with pytest.raises(cleanup.CleanupError, match="План изменился"):
            await cleanup.apply(
                pool, OWNER, op_id, targets, "detach", project_id=project_id, verify=verify_stale)
        assert await pool.fetchval("SELECT count(*) FROM network_nodes WHERE instance_id=$1", project_id) == 2
        planned = await cleanup.plan(pool, OWNER, op_id, targets, "detach", project_id=project_id)

        def verify(current):
            assert current == planned

        await cleanup.apply(pool, OWNER, op_id, targets, "detach", project_id=project_id, verify=verify)
        assert not await pool.fetchval("SELECT id FROM network_edges WHERE id=$1", edge_id)
        assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
        await pool.execute(
            "INSERT INTO workflow_definitions(owner_id,name,is_active,bot_ids) "
            "VALUES($1,'Прогон',TRUE,ARRAY[$2]::bigint[])", OWNER, BOT)
        disabled = await cleanup.plan(pool, OWNER, op_id, [BOT_TARGET], "disable")
        assert disabled["items"][0]["outcome"] == "skipped"
        assert "активные сценарии" in disabled["items"][0]["reason"]

    asyncio.run(_run(scenario))



def test_running_publication_blocks_detach_and_pending_rechecks_targets():
    from services import op_worker

    async def scenario(pool, op_id):
        project_id = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Проект') RETURNING id", OWNER)
        await pool.execute(
            "INSERT INTO network_nodes(instance_id,node_type,ref_id) "
            "VALUES($1,'channel',$2)", project_id, CHANNEL)
        params = {"project_id": project_id, "channel_ids": [CHANNEL], "text": "Обновление проекта"}
        publication_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params) "
            "VALUES($1,'mass_publish','running',$2::jsonb) RETURNING id",
            OWNER, json.dumps(params))
        targets = [CHANNEL_TARGET]
        running = await cleanup.plan(pool, OWNER, op_id, targets, "detach", project_id=project_id)
        assert running["items"][0]["outcome"] == "skipped"
        assert "публикации" in running["items"][0]["reason"]
        await pool.execute("UPDATE operation_queue SET status='pending' WHERE id=$1", publication_id)
        preview = await cleanup.plan(pool, OWNER, op_id, targets, "detach", project_id=project_id)

        def verify(current):
            assert current == preview

        assert (await cleanup.apply(
            pool, OWNER, op_id, targets, "detach", project_id=project_id, verify=verify))["count"] == 1
        publication = await op_worker._exec_mass_publish(pool, None, publication_id, OWNER, params)
        assert publication["status"] == "failed"
        assert "больше не доступны в проекте" in publication["summary"]

    asyncio.run(_run(scenario))


def test_295_findings_and_parallel_confirmation_preserve_other_projects():
    async def scenario(pool, op_id):
        await pool.execute(
            """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, type)
               SELECT $1, 111, id, 'Ресурс ' || id, 'channel' FROM generate_series(1, 295) id""",
            OWNER)
        statuses = ("restricted", "unreachable", "hidden", "check_failed")
        findings = [{"kind": "channel", "id": i, "status": statuses[i % 4]}
                    for i in range(1, 296)]
        await pool.execute("UPDATE operation_queue SET result=$2::jsonb WHERE id=$1", op_id,
                           json.dumps({"findings_version": 2, "findings": findings}))
        project = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Первый') RETURNING id", OWNER)
        other = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Второй') RETURNING id", OWNER)
        await pool.execute(
            """INSERT INTO network_nodes(instance_id,node_type,ref_id)
               VALUES($1,'channel',3),($1,'channel',295),($2,'channel',295)""",
            project, other)

        report = await cleanup.findings(pool, OWNER, op_id)
        assert len(report["findings"]) == 295
        assert {item["status"] for item in report["findings"]} == set(statuses)
        targets = [{"kind": "channel", "id": str(i)} for i in (3, 200, 295)]
        preview = await cleanup.plan(pool, OWNER, op_id, targets, "detach", project_id=project)
        assert [item["outcome"] for item in preview["items"]] == ["ready", "skipped", "ready"]

        def verify(current):
            if current != preview:
                raise cleanup.CleanupError("Подтверждение устарело", 409)

        results = await asyncio.gather(*(
            cleanup.apply(pool, OWNER, op_id, targets, "detach", project_id=project, verify=verify)
            for _ in range(2)
        ), return_exceptions=True)
        successes = [item for item in results if isinstance(item, dict)]
        conflicts = [item for item in results if isinstance(item, cleanup.CleanupError)]
        assert len(successes) == len(conflicts) == 1 and conflicts[0].status == 409
        assert [item["outcome"] for item in successes[0]["items"]] == ["applied", "skipped", "applied"]
        assert await pool.fetchval("SELECT count(*) FROM network_nodes WHERE instance_id=$1", other) == 1
        assert await pool.fetchval("SELECT count(*) FROM network_nodes WHERE instance_id=$1", project) == 0
        assert await pool.fetchval("SELECT count(*) FROM managed_channels WHERE owner_id=$1", OWNER) == 296
        assert await pool.fetchval(
            "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2",
            OWNER, op_id) == 2

        await pool.execute("UPDATE operation_queue SET result=$2::jsonb WHERE id=$1", op_id,
                           json.dumps({"findings": findings[:50]}))
        old = await cleanup.findings(pool, OWNER, op_id)
        assert old["legacy"] and len(old["findings"]) == 50
        with pytest.raises(cleanup.CleanupError, match="нет в результатах"):
            await cleanup.plan(pool, OWNER, op_id, [targets[-1]], "disable")

    asyncio.run(_run(scenario))
