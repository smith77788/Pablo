"""Подписанная сессия Mini App и точное подтверждение на тестовом PostgreSQL."""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from services import mini_app_operations, restriction_cleanup
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import (
    BOT, BOT_TARGET, CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark,
)

__all__ = ["pytestmark"]


def test_authenticated_cleanup_round_trip(monkeypatch):
    secret = "123456:synthetic-cleanup-authentication-only"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, op_id):
        session = "1" + "A" * 180
        await pool.execute("UPDATE managed_bots SET first_name=$1 WHERE bot_id=$2 AND added_by=$3",
                           session, BOT, OWNER)
        await pool.execute("UPDATE operation_queue SET result=$2::jsonb WHERE id=$1", op_id,
                           json.dumps({"findings_version": 2, "findings": [
                               {**BOT_TARGET, "status": "restricted", "reason": f"Ошибка {session}"},
                               {**CHANNEL_TARGET, "status": "check_failed"},
                           ]}))
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        wrong = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        action = {"action": "disable", "targets": [BOT_TARGET]}
        async with TestClient(TestServer(app)) as client:
            assert (await client.get(path)).status == 401
            assert (await client.get(path, headers=wrong)).status == 404
            card = await client.get(f"/api/miniapp/operation/{op_id}", headers=headers)
            assert card.status == 200 and (await card.json())["cleanup_available"]
            findings = await client.get(path, headers=headers)
            assert findings.status == 200
            payload = await findings.json()
            assert {item["id"] for item in payload["findings"]} == {str(BOT), str(CHANNEL)}
            assert "12345:restriction-cleanup-test" not in str(payload)
            masked = next(item for item in payload["findings"] if item["kind"] == "bot")
            assert masked["title"] == "***" and masked["reason"] == "Ошибка ***"
            assert payload["history"] == []
            assert not any("revisions" in item or "bindings" in item for item in payload["findings"])
            assert (await client.post(path, json=action, headers=headers)).status == 400
            for targets in ([], [{"kind": "bot", "id": True}], [BOT_TARGET, BOT_TARGET]):
                invalid = {"action": "disable", "targets": targets, "preview": True}
                assert (await client.post(path, json=invalid, headers=headers)).status == 400
            preview = await client.post(path, json={**action, "preview": True}, headers=headers)
            assert preview.status == 200
            preview_body = await preview.json()
            assert preview_body["items"][0]["reason"] == "Ошибка ***"
            assert "revisions" not in preview_body["items"][0]
            token = preview_body["token"]
            assert secret not in token
            changed = {**action, "targets": [CHANNEL_TARGET], "token": token}
            assert (await client.post(path, json=changed, headers=headers)).status == 409
            confirmation = {**action, "token": token}
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
            assert (await client.post(path, json=confirmation, headers=headers)).status == 403
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
            apply = restriction_cleanup.apply

            async def revoke_before_write(*args, **kwargs):
                with monkeypatch.context() as revoked:
                    revoked.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
                    return await apply(*args, **kwargs)

            with monkeypatch.context() as blocked:
                blocked.setattr(restriction_cleanup, "apply", revoke_before_write)
                assert (await client.post(path, json=confirmation, headers=headers)).status == 403
                assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)

            def fail_before_receipt(_items):
                raise RuntimeError("Синтетический сбой перед сохранением результата")

            with monkeypatch.context() as failed:
                failed.setattr(restriction_cleanup, "public_items", fail_before_receipt)
                assert (await client.post(path, json=confirmation, headers=headers)).status == 500
            assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            for table in ("operation_audit", "restriction_cleanup_receipts"):
                assert not await pool.fetchval(f"SELECT 1 FROM {table} WHERE operation_id=$1", op_id)
            receipt_lookup = restriction_cleanup._receipt
            both_read = asyncio.Event()
            initial_reads = 0

            async def sync_initial_reads(connection, *args):
                result = await receipt_lookup(connection, *args)
                nonlocal initial_reads
                if result is None and initial_reads < 2:
                    initial_reads += 1
                    if initial_reads == 2:
                        both_read.set()
                    await asyncio.wait_for(both_read.wait(), timeout=3)
                return result

            with monkeypatch.context() as concurrent:
                concurrent.setattr(restriction_cleanup, "_receipt", sync_initial_reads)
                responses = await asyncio.gather(*(
                    client.post(path, json=confirmation, headers=headers) for _ in range(2)))
            for response in responses:
                assert response.status == 200, await response.text()
            applied, replayed = sorted([await response.json() for response in responses],
                                      key=lambda body: body.get("replayed", False))
            assert replayed == {**applied, "replayed": True}
            assert applied["changed"][0]["title"] == "***"
            outcome = applied["items"][0]
            assert outcome["id"] == str(BOT) and outcome["outcome"] in ("applied", "disabled")
            assert "revisions" not in outcome
            reread = await (await client.get(path, headers=headers)).json()
            assert [(entry["action"], entry["target"]) for entry in reread["history"]] == [
                ("disable", f"bot:{BOT}")]
            assert reread["history"][0]["occurred_at"]
            assert reread["history_has_more"] is False
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
            assert (await client.get(path, headers=wrong)).status == 404
            assert not await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            repeated = await client.post(path, json=confirmation, headers=headers)
            assert repeated.status == 200, await repeated.text()
            assert await repeated.json() == {**applied, "replayed": True}
            with monkeypatch.context() as revoked:
                revoked.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
                assert (await client.post(path, json=confirmation, headers=headers)).status == 403
            assert (await client.post(path, json=changed, headers=headers)).status == 409
            assert (await client.post(path, json=confirmation, headers=wrong)).status == 409
            await pool.execute("UPDATE managed_bots SET is_active=TRUE WHERE bot_id=$1", BOT)
            again = await (await client.post(path, json=confirmation, headers=headers)).json()
            assert again == {**applied, "replayed": True}
            assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            assert await pool.fetchval(
                "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2",
                OWNER, op_id) == 1
            receipt = await pool.fetchrow(
                "SELECT confirmation_id, response FROM restriction_cleanup_receipts WHERE operation_id=$1",
                op_id)
            assert receipt["confirmation_id"] != token
            assert session not in receipt["response"]
            await pool.execute(
                """INSERT INTO operation_audit(owner_id, operation_id, action, target, result, occurred_at)
                   SELECT $1, $2, 'restriction_cleanup_detach', 'channel:' || n, 'success',
                          now() + n * interval '1 second'
                     FROM generate_series(1, 55) n""", OWNER, op_id)
            try:
                await pool.execute(
                    """INSERT INTO operation_audit(owner_id, operation_id, action, target, result)
                       VALUES($1, $2, 'restriction_cleanup_disable', 'foreign', 'success'),
                             ($3, $2, 'other_action', 'unrelated', 'success')""", OWNER + 1, op_id, OWNER)
                capped = await (await client.get(path, headers=headers)).json()
                assert capped["history_has_more"] is True
                assert len(capped["history"]) == 50
                assert capped["history"][0]["target"] == "channel:55"
                assert capped["history"][-1]["target"] == "channel:6"
            finally:
                await pool.execute("DELETE FROM operation_audit WHERE owner_id=$1 AND operation_id=$2",
                                   OWNER + 1, op_id)


    asyncio.run(_run(scenario))


@pytest.mark.parametrize("changed", ["node", "edge"])
def test_detach_stale_catalog_link_through_authenticated_preview(monkeypatch, changed):
    secret = "123456:synthetic-stale-link"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))

    async def scenario(pool, op_id):
        projects = await pool.fetch(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Первый'),($1,'Второй') RETURNING id",
            OWNER)
        project, other = (row["id"] for row in projects)
        await pool.execute(
            """INSERT INTO network_nodes(instance_id,node_type,ref_id)
               VALUES($1,'channel',$3),($2,'channel',$3)""", project, other, CHANNEL)
        node = await pool.fetchval(
            "SELECT id FROM network_nodes WHERE instance_id=$1 AND ref_id=$2", project, CHANNEL)
        anchor = await pool.fetchval(
            "INSERT INTO network_nodes(instance_id,node_type) VALUES($1,'bot') RETURNING id", project)
        edge = await pool.fetchval(
            """INSERT INTO network_edges(instance_id,source_node_id,target_node_id)
               VALUES($1,$2,$3) RETURNING id""", project, node, anchor)
        await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL)
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        async with TestClient(TestServer(app)) as client:
            result = await (await client.get(path, headers=headers)).json()
            stale = next(item for item in result["findings"] if item["id"] == str(CHANNEL))
            assert stale["active"] is False and stale["can_disable"] is False
            assert {item["id"] for item in stale["projects"]} == {project, other}
            assert "can_restore" not in stale
            body = {"action": "detach", "project_id": str(project), "targets": [CHANNEL_TARGET]}
            response = await client.post(path, json={**body, "preview": True}, headers=headers)
            assert response.status == 200, await response.text()
            preview = await response.json()
            assert preview["items"][0]["outcome"] == "ready"
            assert preview["items"][0]["status"] == "check_failed"
            assert "node_revisions" not in preview["items"][0]
            assert "edge_revisions" not in preview["items"][0]
            if changed == "node":
                await pool.execute("UPDATE network_nodes SET label='Новая метка' WHERE id=$1", node)
            else:
                await pool.execute("UPDATE network_edges SET edge_type='copy' WHERE id=$1", edge)
            rejected = await client.post(path, json={**body, "token": preview["token"]}, headers=headers)
            assert rejected.status == 409, await rejected.text()
            assert await pool.fetchval("SELECT 1 FROM network_edges WHERE id=$1", edge)
            assert not await pool.fetchval("SELECT 1 FROM operation_audit WHERE operation_id=$1", op_id)
            response = await client.post(path, json={**body, "preview": True}, headers=headers)
            preview = await response.json()
            response = await client.post(path, json={**body, "token": preview["token"]}, headers=headers)
            assert response.status == 200, await response.text()
            assert (await response.json())["items"][0]["outcome"] == "applied"
            assert await pool.fetchval("SELECT instance_id FROM network_nodes WHERE ref_id=$1", CHANNEL) == other
            assert await pool.fetchval("SELECT 1 FROM network_nodes WHERE id=$1", anchor)
            assert not await pool.fetchval("SELECT 1 FROM network_edges WHERE id=$1", edge)
            replay = await client.post(path, json={**body, "token": preview["token"]}, headers=headers)
            assert replay.status == 200 and (await replay.json())["replayed"] is True
            wrong_project = await client.post(path, json={**body, "project_id": str(other),
                                                          "token": preview["token"]}, headers=headers)
            assert wrong_project.status == 409

    asyncio.run(_run(scenario))


def test_removed_channel_can_be_restored_by_exact_id_through_api(monkeypatch):
    from unittest.mock import AsyncMock

    from bot.utils import subscription
    from services import mini_app_channels

    secret = "123456:synthetic-channel-restore-only"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
    monkeypatch.setattr(subscription, "get_channel_limit", AsyncMock(return_value=10))
    monkeypatch.setattr(subscription, "get_effective_channel_count", AsyncMock(return_value=0))

    async def scenario(pool, op_id):
        app = web.Application()
        mini_app_channels.setup_routes(app, pool)
        mini_app_operations.setup_routes(app, pool)
        owner = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        other = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        async with TestClient(TestServer(app)) as client:
            report_path = f"/api/miniapp/operations/{op_id}/cleanup"
            action = {"action": "disable", "targets": [CHANNEL_TARGET]}
            preview = await client.post(report_path, json={**action, "preview": True}, headers=owner)
            assert preview.status == 200, await preview.text()
            completed = await client.post(report_path, json={**action, "token": (await preview.json())["token"]},
                                          headers=owner)
            assert completed.status == 200, await completed.text()
            await pool.execute(
                """INSERT INTO removed_channels(owner_id, channel_id, acc_id, reason, title, type, removed_at)
                   SELECT $1, n, 111, 'owner', 'Другой ресурс', 'channel', now()+interval '1 second'
                     FROM generate_series(1, 501) n""", OWNER)
            removed = await client.get("/api/miniapp/channels/removed", headers=owner)
            assert all(row["channel_id_text"] != str(CHANNEL) for row in (await removed.json())["channels"])
            listing = "/api/miniapp/channels/removed"
            for bad in ("0", "oops", str(2**63), "1&channel_id=2"):
                assert (await client.get(listing + "?channel_id=" + bad, headers=owner)).status == 400
            exact = listing + "?channel_id=" + str(CHANNEL)
            assert (await (await client.get(exact, headers=other)).json())["channels"] == []
            rows = (await (await client.get(exact, headers=owner)).json())["channels"]
            assert len(rows) == 1
            item = rows[0]
            assert item["channel_id"] == CHANNEL and item["channel_id_text"] == str(CHANNEL)
            assert "catalog_metadata" not in item and "access_hash" not in item
            path = f"/api/miniapp/channel/{item['channel_id_text']}/restore"
            assert (await client.post(path, headers=other)).status == 404
            response = await client.post(path, headers=owner)
            assert response.status == 200, await response.text()
            assert (await (await client.get(exact, headers=owner)).json())["channels"] == []
            assert (await client.post(f"/api/miniapp/channel/{2**63}/restore", headers=owner)).status == 400
            assert (await client.post(path, headers=owner)).status == 404
            original = await client.get(report_path, headers=owner)
            report = await original.json()
            assert [(entry["action"], entry["target"]) for entry in report["history"]] == [
                ("restore", str(CHANNEL)), ("disable", f"channel:{CHANNEL}")]
            assert (await client.get(report_path, headers=other)).status == 404

    asyncio.run(_run(scenario))


def test_restore_serializes_last_slot_and_rolls_back_without_audit(monkeypatch):
    from unittest.mock import AsyncMock

    from bot.utils import subscription
    from database import db
    from services import mini_app_channels

    secret = "123456:synthetic-restore-concurrency"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setattr(subscription, "get_channel_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(subscription, "get_plan", AsyncMock(return_value="free"))
    other_id, no_binding = CHANNEL + 1, CHANNEL + 2

    async def scenario(pool, op_id):
        assert await db.remove_managed_channel(pool, OWNER, CHANNEL)
        await pool.execute(
            """INSERT INTO removed_channels(owner_id, channel_id, acc_id, reason, title, type)
               VALUES ($1,$2,111,'owner','Второй','channel'),
                      ($1,$3,NULL,'owner','Старая запись','channel')""",
            OWNER, other_id, no_binding,
        )
        await pool.execute(
            """UPDATE operation_queue SET result=$2::jsonb WHERE id=$1""",
            op_id, json.dumps({"findings_version": 2, "findings": [
                {"kind": "channel", "id": str(no_binding), "status": "unreachable"},
            ]}),
        )
        report = await restriction_cleanup.findings(pool, OWNER, op_id)
        assert not report["findings"][0]["can_restore"]
        assert report["findings"][0]["status"] == "unreachable"

        app = web.Application()
        mini_app_channels.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = "/api/miniapp/channel/{}/restore"
        lock_catalog = db.lock_managed_channel_catalog
        both_ready = asyncio.Event()
        requests = 0

        async def meet_before_lock(conn, owner):
            nonlocal requests
            requests += 1
            if requests == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=3)
            await lock_catalog(conn, owner)

        async with TestClient(TestServer(app)) as client:
            with monkeypatch.context() as concurrent:
                concurrent.setattr(db, "lock_managed_channel_catalog", meet_before_lock)
                responses = await asyncio.gather(*(
                    client.post(path.format(channel), headers=headers) for channel in (CHANNEL, other_id)
                ))
            assert sorted(response.status for response in responses) == [200, 403]
            winner, loser = (CHANNEL, other_id) if responses[0].status == 200 else (other_id, CHANNEL)
            assert await pool.fetchval("SELECT channel_id FROM managed_channels WHERE owner_id=$1", OWNER) == winner
            audit = await pool.fetch(
                "SELECT target FROM operation_audit WHERE owner_id=$1 AND action='channel_restore'", OWNER)
            assert [row["target"] for row in audit] == [str(winner)]
            assert (await client.post(path.format(winner), headers=headers)).status == 404
            assert (await client.post(path.format(no_binding), headers=headers)).status == 404

            await db.remove_managed_channel(pool, OWNER, winner)
            await pool.execute(f"""ALTER TABLE operation_audit ADD CONSTRAINT synthetic_restore_failure
                CHECK (owner_id <> {OWNER} OR action <> 'channel_restore' OR target <> '{loser}') NOT VALID""")
            try:
                assert (await client.post(path.format(loser), headers=headers)).status == 500
                assert not await pool.fetchval("SELECT 1 FROM managed_channels WHERE owner_id=$1", OWNER)
                assert await pool.fetchval(
                    "SELECT 1 FROM removed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, loser)
            finally:
                await pool.execute("ALTER TABLE operation_audit DROP CONSTRAINT synthetic_restore_failure")
            assert (await client.post(path.format(loser), headers=headers)).status == 200
            assert await pool.fetchval(
                "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND action='channel_restore'", OWNER) == 2

    asyncio.run(_run(scenario))
