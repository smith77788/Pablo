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
        for name in ("schema_v371_restriction_cleanup.sql", "schema_v372_removed_channel_metadata.sql"):
            await pool.execute(Path(name).read_text(encoding="utf-8"))
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


@pytest.mark.parametrize("complete", [False, True])
def test_overlapping_import_cannot_reactivate_disabled_channel(complete, monkeypatch):
    async def scenario(pool, op_id):
        entered = asyncio.Event()
        original_lock = db.lock_managed_channel_catalog
        async with pool.acquire() as conn:
            async with conn.transaction():
                await original_lock(conn, OWNER)

                async def observe_lock(connection, owner_id):
                    entered.set()
                    await original_lock(connection, owner_id)

                monkeypatch.setattr(db, "lock_managed_channel_catalog", observe_lock)
                importing = asyncio.create_task(db.upsert_managed_channels(
                    pool, OWNER, 111, [{"id": CHANNEL, "title": "Результат скана"},
                                       {"id": 123, "title": "Другой канал"}], complete=complete))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    assert not importing.done()
                    assert await db.remove_managed_channel_on_connection(
                        conn, OWNER, CHANNEL, reason=db.REMOVED_BY_OWNER, detail="Отключение")
                except BaseException:
                    importing.cancel()
                    await asyncio.gather(importing, return_exceptions=True)
                    raise
            assert await asyncio.wait_for(importing, 3) == 1

        rows = await pool.fetch(
            "SELECT channel_id FROM managed_channels WHERE owner_id=$1", OWNER)
        assert [row["channel_id"] for row in rows] == [123]
        assert (await db.list_removed_channels(pool, OWNER))[0]["channel_id"] == CHANNEL

    asyncio.run(_run(scenario))


def test_restore_preserves_catalog_identity_roles_and_statistics():
    async def scenario(pool, op_id):
        columns = "id, added_at, access_hash, about, last_drift_check, members_count, avg_views, influence, is_admin, is_creator"
        before = await pool.fetchrow(
            f"""UPDATE managed_channels SET access_hash=991, about='Описание',
                      last_drift_check=now(), members_count=412, avg_views=200,
                      influence=5.25, is_admin=TRUE, is_creator=FALSE
                 WHERE owner_id=$1 AND channel_id=$2 RETURNING {columns}""", OWNER, CHANNEL)
        assert await db.remove_managed_channel(pool, OWNER, CHANNEL)
        assert not await db.restore_removed_channel(pool, OWNER + 1, CHANNEL)
        assert await db.restore_removed_channel(pool, OWNER, CHANNEL)
        after = await pool.fetchrow(
            f"SELECT {columns} FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL)
        assert dict(after) == dict(before)

        await pool.execute(
            """INSERT INTO removed_channels(owner_id, channel_id, reason, acc_id, title)
               VALUES($1, 42, 'owner', 111, 'Старый канал')""", OWNER)
        assert await db.restore_removed_channel(pool, OWNER, 42)
        old = await pool.fetchrow(
            "SELECT id, title, members_count, added_at FROM managed_channels WHERE owner_id=$1 AND channel_id=42",
            OWNER)
        assert old["id"] != before["id"] and old["title"] == "Старый канал"
        assert old["members_count"] == 0 and old["added_at"] is not None

    asyncio.run(_run(scenario))


def test_reconnect_keeps_tombstone_on_write_failure():
    async def scenario(pool, op_id):
        assert await db.remove_managed_channel(pool, OWNER, CHANNEL)
        with pytest.raises(asyncpg.DataError):
            await db.add_managed_channels(
                pool, OWNER, 2**40, [{"id": CHANNEL, "title": "Явное подключение"}], reconnect=True)
        assert await db.add_managed_channels(pool, OWNER, 111, [{"id": CHANNEL}]) == 0
        assert await db.add_managed_channels(
            pool, OWNER, 111, [{"id": CHANNEL, "title": "Явное подключение"}], reconnect=True) == 1
        assert await db.list_removed_channels(pool, OWNER) == []

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("kind,worker_name", [
    ("channel", "_exec_channel_import_all"), ("megagroup", "_exec_group_import_all"),
])
def test_import_worker_reports_only_saved_channels(kind, worker_name, monkeypatch):
    from unittest.mock import AsyncMock

    from services import account_manager, op_worker

    async def scenario(pool, op_id):
        await db.remove_managed_channel(pool, OWNER, CHANNEL)
        dialogs = [
            {"id": CHANNEL, "type": kind, "is_admin": True},
            {"id": 456, "type": kind, "is_creator": True},
            {"id": 789, "type": kind, "is_admin": False},
        ]
        monkeypatch.setattr(op_worker.resource_selector, "select_all_active",
                            AsyncMock(return_value=[{"id": 111, "session_str": "synthetic"}]))
        monkeypatch.setattr(op_worker, "try_claim_accounts", AsyncMock(return_value=[111]))
        monkeypatch.setattr(op_worker, "release_accounts", AsyncMock())
        monkeypatch.setattr(op_worker, "_is_cancelled", AsyncMock(return_value=False))
        monkeypatch.setattr(account_manager, "get_dialogs", AsyncMock(return_value=dialogs))
        result = await getattr(op_worker, worker_name)(pool, None, op_id, OWNER, {"account_ids": [111]})
        assert result["imported"] == result["disabled_skipped"] == result["foreign_skipped"] == 1
        assert "Отключены в Infragram: 1" in result["summary"]
        assert await pool.fetchval("SELECT channel_id FROM managed_channels WHERE owner_id=$1", OWNER) == 456

    asyncio.run(_run(scenario))


def test_native_scan_uses_the_same_catalog_guard(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers import accounts
    from bot.utils import subscription

    async def scenario(pool, op_id):
        await db.remove_managed_channel(pool, OWNER, CHANNEL)
        monkeypatch.setattr(db, "get_tg_account", AsyncMock(return_value={"session_str": "synthetic"}))
        monkeypatch.setattr(accounts, "scan_owned_assets", AsyncMock(return_value={
            "channels": [{"id": CHANNEL, "title": "Отключённый канал"}],
            "groups": [{"id": 456, "title": "Группа", "type": "megagroup"}],
        }))
        monkeypatch.setattr(subscription, "get_channel_limit", AsyncMock(return_value=100))
        monkeypatch.setattr(subscription, "get_effective_channel_count", AsyncMock(return_value=0))
        keyboard = MagicMock()
        monkeypatch.setattr(accounts, "InlineKeyboardBuilder", lambda: keyboard)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=OWNER), answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        await accounts.cb_scan_connect(
            callback, SimpleNamespace(acc_id=111, action="scan_connect_all"), pool)
        assert "Подключено: 1" in callback.message.edit_text.await_args.args[0]
        row = await pool.fetchrow("SELECT channel_id, type FROM managed_channels WHERE owner_id=$1", OWNER)
        assert (row["channel_id"], row["type"]) == (456, "megagroup")
        db.get_tg_account.assert_awaited_once_with(pool, 111, OWNER)

    asyncio.run(_run(scenario))
