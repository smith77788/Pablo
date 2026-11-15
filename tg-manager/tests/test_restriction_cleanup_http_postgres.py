"""Подписанная сессия Mini App и точное подтверждение на тестовом PostgreSQL."""
import asyncio
import json
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from services import account_manager, mini_app_operations, op_worker, restriction_cleanup
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import (
    BOT, BOT_TARGET, CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark,
)

__all__ = ["pytestmark"]


@pytest.mark.parametrize("change", ["disable", "reassign"])
def test_queued_channel_edit_revalidates_targets_after_cleanup(monkeypatch, change):
    secret = "123456:synthetic-cleanup-authentication-only"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    editor = AsyncMock(return_value=True)
    monkeypatch.setattr(account_manager, "edit_channel_about", editor)
    monkeypatch.setattr(op_worker, "try_claim_accounts", AsyncMock(return_value=[992660]))
    monkeypatch.setattr(op_worker, "release_accounts", AsyncMock())

    async def scenario(pool, check_id):
        other = CHANNEL + 1
        foreign = CHANNEL + 2
        await pool.execute(
            """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, type)
               VALUES($1,992660,$2,'Другой канал','channel'),
                     ($3,992660,$4,'Чужой канал','channel')""",
            OWNER, other, OWNER + 1, foreign)
        params = {"op": "chan_about", "value": "Синтетическое описание", "channel_acc_pairs": [
            {"channel_id": str(CHANNEL), "acc_id": "992660"},
            {"channel_id": other, "acc_id": 992660},
            {"channel_id": foreign, "acc_id": 992660},
        ]}
        op_id = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id,op_type,status,params)
               VALUES($1,'bulk_chan_exec','pending',$2::jsonb) RETURNING id""",
            OWNER, json.dumps(params))
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{check_id}/cleanup"
        action = {"action": "disable", "targets": [CHANNEL_TARGET]}
        try:
            async with TestClient(TestServer(app)) as client:
                preview = await client.post(path, headers=headers, json={**action, "preview": True})
                assert preview.status == 200, await preview.text()
                confirmation = {**action, "token": (await preview.json())["token"]}
                if change == "disable":
                    applied = await client.post(path, headers=headers, json=confirmation)
                    assert applied.status == 200 and (await applied.json())["count"] == 1
                    report = await (await client.get(path, headers=headers)).json()
                    item = next(item for item in report["findings"] if item["id"] == str(CHANNEL))
                    assert item["status"] == "check_failed" and item["can_restore"]
                else:
                    await pool.execute(
                        "UPDATE managed_channels SET acc_id=992661 WHERE owner_id=$1 AND channel_id=$2",
                        OWNER, CHANNEL)
                    assert (await client.post(path, headers=headers, json=confirmation)).status == 409

                excluded = {row["id"] for row in await pool.fetch(
                    "SELECT id FROM operation_queue WHERE status='pending' AND id<>$1", op_id)}
                monkeypatch.setattr(op_worker, "_active_op_ids", excluded)
                monkeypatch.setattr(op_worker, "_active_op_tasks", set())
                monkeypatch.setattr(op_worker, "_MAX_PARALLEL", len(excluded) + 2)
                await op_worker._process_pending(pool, None)
                tasks = list(op_worker._active_op_tasks)
                assert [task.get_name() for task in tasks] == [f"op:{op_id}"]
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
                editor.assert_awaited_once()
                assert editor.await_args.args[1] == other

                card_path = f"/api/miniapp/operation/{op_id}"
                card = await (await client.get(card_path, headers=headers)).json()
                assert card["status"] == "partial"
                journal = await (await client.get(card_path + "/log", headers=headers)).json()
                assert await pool.fetchval(
                    "SELECT count(*) FROM operation_log WHERE op_id=$1 AND status='skipped'", op_id) == 2
                assert {str(CHANNEL), str(other), str(foreign)} <= {
                    row["target"] for row in journal["logs"]}
                outside = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
                assert (await client.get(card_path + "/log", headers=outside)).status == 404

                # Повтор не делает второй сетевой вызов и не дублирует отметки о пропуске.
                replay = await op_worker._exec_bulk_chan_exec(pool, None, op_id, OWNER, params)
                assert (replay["ok"], replay["fail"]) == (1, 2)
                editor.assert_awaited_once()
                assert await pool.fetchval(
                    "SELECT count(*) FROM operation_log WHERE op_id=$1 AND status='skipped'", op_id) == 2
        finally:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                               OWNER + 1, foreign)

    asyncio.run(_run(scenario))


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
            for invalid_preview in (True, "true", 1, None):
                refused = await client.post(path, json={**confirmation, "preview": invalid_preview}, headers=headers)
                assert refused.status == 400
            assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            assert not await pool.fetchval("SELECT 1 FROM operation_audit WHERE operation_id=$1", op_id)
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


def test_old_report_keeps_missing_findings_without_expanding_cleanup(monkeypatch):
    secret = "123456:synthetic-history-only"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
    outcomes = ("unreachable", "restricted", "hidden", "check_failed")
    missing = [{"kind": "channel", "id": str(CHANNEL + i), "status": status,
                "title": f"Историческая находка {i}", "reason": "Исходная причина"}
               for i, status in enumerate(outcomes, 1)]
    targets = [{key: item[key] for key in ("kind", "id")} for item in missing]

    async def scenario(pool, op_id):
        await pool.execute("UPDATE operation_queue SET status='partial', result=$2::jsonb WHERE id=$1",
                           op_id, json.dumps({"findings": [
                               *missing, {**BOT_TARGET, "status": "restricted"}
                           ]}))
        await pool.execute(
            "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, type) "
            "VALUES($1,992660,$2,'Закрытое название другого владельца','channel')",
            OWNER + 1, int(missing[0]["id"]))
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        other = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        try:
            async with TestClient(TestServer(app)) as client:
                assert (await client.get(path, headers=other)).status == 404
                report = await (await client.get(path, headers=headers)).json()
                assert report["legacy"]
                assert report["findings"][:4] == [
                    {**item, "active": False, "can_disable": False, "projects": [],
                     "disable_reason": "Ресурс отсутствует в вашем каталоге"}
                    for item in missing
                ]
                action = {"action": "disable", "targets": targets}
                unavailable = await client.post(path, json={**action, "preview": True}, headers=headers)
                assert unavailable.status == 200
                preview = await unavailable.json()
                assert preview["token"] is None
                assert [item["status"] for item in preview["items"]] == list(outcomes)
                assert all(item["outcome"] == "skipped" and item["finding_reason"] == "Исходная причина"
                           for item in preview["items"])
                assert (await client.post(path, json=action, headers=headers)).status == 400
                action["targets"] += [BOT_TARGET]
                preview = await (await client.post(path, json={**action, "preview": True}, headers=headers)).json()
                applied = await client.post(path, json={**action, "token": preview["token"]}, headers=headers)
                assert applied.status == 200, await applied.text()
                result = await applied.json()
                assert [item["id"] for item in result["changed"]] == [str(BOT)]
                assert [item["status"] for item in result["items"] if item["outcome"] == "skipped"] == list(outcomes)
                assert await pool.fetchval(
                    "SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                    OWNER + 1, int(missing[0]["id"]))
                reread = await (await client.get(path, headers=headers)).json()
                assert reread["findings"][:4] == report["findings"][:4]
                assert [entry["target"] for entry in reread["history"]] == [f"bot:{BOT}"]
        finally:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                               OWNER + 1, int(missing[0]["id"]))

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


def test_legacy_group_topology_recovers_only_owner_links(monkeypatch):
    secret = "123456:synthetic-legacy-group"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, op_id):
        other_owner = OWNER + 1
        project, neighbor, foreign = [row["id"] for row in await pool.fetch(
            """INSERT INTO network_instances(owner_id,name)
               VALUES($1,'Основной'),($1,'Соседний'),($2,'Чужой') RETURNING id""",
            OWNER, other_owner)]
        conflict, known = CHANNEL + 1, CHANNEL + 2
        try:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL)
            await pool.execute(
                """INSERT INTO managed_channels(owner_id,acc_id,channel_id,type,title)
                   VALUES($1,992660,$2,'channel','Обычный канал'),($3,992660,$4,'channel','Чужой ресурс')""",
                OWNER, known, other_owner, CHANNEL)
            await pool.execute(
                """INSERT INTO network_nodes(instance_id,node_type,ref_id)
                   VALUES($1,'group',$4),($2,'gigagroup',$4),($3,'channel',$4),
                         ($1,'group',$5),($2,'channel',$5),($1,'group',$6)""",
                project, neighbor, foreign, CHANNEL, conflict, known)
            await pool.execute("UPDATE operation_queue SET result=$2::jsonb WHERE id=$1", op_id,
                               json.dumps({"findings": [
                                   {"kind": "channel", "id": CHANNEL, "status": "hidden",
                                    "reason": "Скрыта из поиска"},
                                   {"kind": "channel", "id": conflict, "status": "restricted"},
                                   {"kind": "channel", "id": known, "status": "check_failed"},
                               ]}))
            headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
            app = web.Application()
            mini_app_operations.setup_routes(app, pool)
            path = f"/api/miniapp/operations/{op_id}/cleanup"
            target = {"kind": "group", "id": str(CHANNEL)}
            async with TestClient(TestServer(app)) as client:
                report = await (await client.get(path, headers=headers)).json()
                assert report["legacy"]
                group, ambiguous, catalog = report["findings"]
                assert group["kind"] == "group" and group["id"] == str(CHANNEL)
                assert group["status"] == "hidden" and group["reason"] == "Скрыта из поиска"
                assert not group["can_disable"] and not group.get("can_restore")
                assert {p["id"] for p in group["projects"]} == {project, neighbor}
                assert ambiguous["kind"] == "channel" and ambiguous["projects"][0]["id"] == neighbor
                assert catalog["kind"] == "channel" and catalog["projects"] == []
                payload = {"action": "detach", "project_id": project, "targets": [target]}
                for target_id, code in ((conflict, 409), (known, 409)):
                    rejected = await client.post(path, headers=headers, json={
                        **payload, "preview": True, "targets": [{**target, "id": str(target_id)}],
                    })
                    assert rejected.status == code
                assert (await client.post(path, headers=headers, json={
                    **payload, "preview": True, "project_id": foreign})).status == 404
                preview = await (await client.post(path, headers=headers,
                                                  json={**payload, "preview": True})).json()
                assert preview["items"][0]["outcome"] == "ready"
                signed = {**payload, "token": preview["token"]}
                await pool.execute("UPDATE network_nodes SET node_type='channel' WHERE instance_id=$1 AND ref_id=$2",
                                   neighbor, CHANNEL)
                assert (await client.post(path, headers=headers, json=signed)).status == 409
                await pool.execute("UPDATE network_nodes SET node_type='gigagroup' WHERE instance_id=$1 AND ref_id=$2",
                                   neighbor, CHANNEL)
                confirmed = await client.post(path, headers=headers, json=signed)
                assert confirmed.status == 200, await confirmed.text()
                assert (await confirmed.json())["count"] == 1
                rows = await pool.fetch("SELECT instance_id FROM network_nodes WHERE ref_id=$1", CHANNEL)
                assert {row["instance_id"] for row in rows} == {neighbor, foreign}
                replay = await (await client.post(path, headers=headers, json=signed)).json()
                assert replay["replayed"]
                reread = await (await client.get(path, headers=headers)).json()
                assert reread["history"][0]["target"] == f"group:{CHANNEL} project:{project}"
                assert reread["findings"][0]["projects"][0]["id"] == neighbor
                last_link = {**payload, "project_id": neighbor}
                last_preview = await (await client.post(path, headers=headers,
                                                       json={**last_link, "preview": True})).json()
                final = await client.post(path, headers=headers,
                                          json={**last_link, "token": last_preview["token"]})
                assert final.status == 200, await final.text()
                assert (await final.json())["count"] == 1
                history_only = await (await client.get(path, headers=headers)).json()
                assert history_only["findings"][0]["kind"] == "group"
                assert history_only["findings"][0]["status"] == "hidden"
                assert history_only["findings"][0]["projects"] == []
                assert len(history_only["history"]) == 2
                assert await pool.fetchval("SELECT instance_id FROM network_nodes WHERE ref_id=$1", CHANNEL) == foreign
        finally:
            await pool.execute("DELETE FROM network_instances WHERE id=$1 AND owner_id=$2", foreign, other_owner)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", other_owner, CHANNEL)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("change", ["disabled", "foreign", "deleted", "empty_session"])
def test_channel_restore_rechecks_account_after_preview(monkeypatch, change):
    from database import db
    from services import mini_app_channels

    secret = "123456:synthetic-restore-binding"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    account = 992660

    async def scenario(pool, op_id):
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        mini_app_channels.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        cleanup_path = f"/api/miniapp/operations/{op_id}/cleanup"
        listing = f"/api/miniapp/channels/removed?channel_id={CHANNEL}"
        restore = f"/api/miniapp/channel/{CHANNEL}/restore"
        async with TestClient(TestServer(app)) as client:
            action = {"action": "disable", "targets": [CHANNEL_TARGET]}
            preview = await (await client.post(cleanup_path, json={**action, "preview": True},
                                              headers=headers)).json()
            disabled = await client.post(cleanup_path, json={**action, "token": preview["token"]}, headers=headers)
            assert disabled.status == 200, await disabled.text()
            assert (await (await client.get(listing, headers=headers)).json())["channels"][0]["can_restore"]
            try:
                if change == "deleted":
                    await pool.execute("DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2", account, OWNER)
                else:
                    await pool.execute(
                        """UPDATE tg_accounts SET owner_id=$2, is_active=$3, session_str=$4
                            WHERE id=$1 AND owner_id=$5""",
                        account, OWNER + (change == "foreign"), change != "disabled",
                        "" if change == "empty_session" else "synthetic-unavailable-session", OWNER)
                failed = await client.post(restore, headers=headers)
                assert failed.status == 409, await failed.text()
                item = (await (await client.get(listing, headers=headers)).json())["channels"][0]
                assert item["channel_id_text"] == str(CHANNEL) and not item["can_restore"]
                assert "аккаунт больше не доступен" in item["restore_reason"]
                report = await (await client.get(cleanup_path, headers=headers)).json()
                finding = next(item for item in report["findings"] if item["id"] == str(CHANNEL))
                assert finding["status"] == "check_failed" and not finding["can_restore"]
                assert finding["restore_reason"] == item["restore_reason"]
                assert "synthetic-unavailable-session" not in str(report)
                assert [event["action"] for event in report["history"]] == ["disable"]
                assert not await pool.fetchval("SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                                              OWNER, CHANNEL)
                assert await db.add_managed_channels(pool, OWNER, account, [CHANNEL_TARGET]) == 0
            finally:
                await pool.execute(
                    """INSERT INTO tg_accounts(id, owner_id, phone, session_str)
                       VALUES($1,$2,'synthetic-cleanup','synthetic-restore-session')
                       ON CONFLICT(id) DO UPDATE SET owner_id=EXCLUDED.owner_id, is_active=TRUE,
                                                     session_str=EXCLUDED.session_str""", account, OWNER)
            succeeded = await client.post(restore, headers=headers)
            assert succeeded.status == 200, await succeeded.text()
            report = await (await client.get(cleanup_path, headers=headers)).json()
            assert [event["action"] for event in report["history"]] == ["restore", "disable"]

    asyncio.run(_run(scenario))


def test_removed_channel_can_be_restored_by_exact_id_through_api(monkeypatch):

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
                   SELECT $1, n, 992660, 'owner', 'Другой ресурс', 'channel', now()+interval '1 second'
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
               VALUES ($1,$2,992660,'owner','Второй','channel'),
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


@pytest.mark.parametrize("competitor", ["restore", "scan"])
def test_linked_owners_compete_for_one_catalog_slot(monkeypatch, competitor):

    from bot.utils import subscription
    from database import db
    from services import mini_app_channels

    secret = "123456:synthetic-linked-catalog-quota"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setattr(subscription, "get_channel_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(subscription, "get_plan", AsyncMock(return_value="free"))
    linked, other_channel = OWNER + 1, CHANNEL + 1

    async def scenario(pool, _op_id):
        assert await db.remove_managed_channel(pool, OWNER, CHANNEL)
        await pool.execute(
            "INSERT INTO linked_platform_users(owner_id_a, owner_id_b) VALUES($1,$2)", OWNER, linked)
        try:
            await pool.execute(
                "INSERT INTO tg_accounts(id, owner_id, phone, session_str) "
                "VALUES(992661,$1,'synthetic-linked-cleanup','synthetic-linked-session')", linked)
            await pool.execute(
                """INSERT INTO removed_channels(owner_id, channel_id, acc_id, reason, title, type)
                   VALUES($1,$2,992661,$3,'Второй','megagroup')""",
                linked, other_channel, "gone" if competitor == "scan" else "owner",
            )
            app = web.Application()
            mini_app_channels.setup_routes(app, pool)
            path = "/api/miniapp/channel/{}/restore"

            def headers(owner):
                return {"Authorization": f"Bearer {make_token(owner, secret)}"}

            ready = asyncio.Event()
            entrants = 0
            original_lock = db._lock_managed_channel_quota

            async def concurrent_quota(conn, owner):
                nonlocal entrants
                entrants += 1
                if entrants == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=3)
                await original_lock(conn, owner)

            async with TestClient(TestServer(app)) as client:
                async def compete():
                    if competitor == "restore":
                        response = await client.post(path.format(other_channel), headers=headers(linked))
                        return response.status
                    try:
                        imported, skipped = await db.connect_scanned_managed_channels(
                            pool, linked, 992661, [{"id": other_channel, "type": "megagroup"}])
                    except db.ManagedChannelLimitError:
                        return 403
                    assert (imported, skipped) == (1, 0)
                    return 200

                with monkeypatch.context() as race:
                    race.setattr(db, "_lock_managed_channel_quota", concurrent_quota)
                    left, right = await asyncio.wait_for(asyncio.gather(
                        client.post(path.format(CHANNEL), headers=headers(OWNER)), compete(),
                    ), timeout=6)
                assert sorted((left.status, right)) == [200, 403]
                accepted, rejected = ((CHANNEL, other_channel) if left.status == 200
                                      else (other_channel, CHANNEL))
                assert await pool.fetchval(
                    "SELECT channel_id FROM managed_channels WHERE owner_id IN ($1,$2)", OWNER, linked,
                ) == accepted
                rows = await pool.fetch(
                    "SELECT channel_id FROM removed_channels WHERE owner_id IN ($1,$2)", OWNER, linked,
                )
                assert [row["channel_id"] for row in rows] == [rejected]
        finally:
            await pool.execute("DELETE FROM operation_audit WHERE owner_id=$1", linked)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", linked)
            await pool.execute("DELETE FROM removed_channels WHERE owner_id=$1", linked)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1 AND id=992661", linked)
            await pool.execute("DELETE FROM linked_platform_users WHERE owner_id_a=$1 AND owner_id_b=$2",
                               OWNER, linked)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("profile_count", [295, 1000])
def test_cleanup_large_report_through_signed_http(monkeypatch, profile_count):
    import math
    from time import perf_counter

    from database import db

    secret = "123456:synthetic-large-cleanup"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    statuses = ("restricted", "unreachable", "hidden", "check_failed")

    async def scenario(pool, op_id):
        profiles = await pool.fetch(
            """INSERT INTO tg_accounts(owner_id,phone,session_str)
               SELECT $1, 'synthetic-cleanup-scale-' || n, 'synthetic-scale-session'
                 FROM generate_series(1,$2::int) n RETURNING id""", OWNER, profile_count)
        account_ids = [row["id"] for row in profiles]
        try:
            await pool.execute(
                """INSERT INTO managed_channels(owner_id,acc_id,channel_id,type,title)
                   SELECT $1, aid, $2::bigint+ordinal,
                          CASE WHEN ordinal=$4 THEN 'gigagroup' ELSE 'channel' END,
                          'Синтетический ресурс ' || ordinal
                     FROM unnest($3::bigint[]) WITH ORDINALITY AS p(aid,ordinal)""",
                OWNER, CHANNEL, account_ids, profile_count,
            )
            last = CHANNEL + profile_count
            findings = [
                {"kind": "channel", "id": CHANNEL + n, "status": statuses[(n - 1) % len(statuses)]}
                for n in range(1, profile_count + 1)
            ]
            await pool.execute("UPDATE operation_queue SET result=$2::jsonb WHERE id=$1", op_id,
                               json.dumps({"findings_version": 2, "findings": findings}))
            projects = await pool.fetch(
                """INSERT INTO network_instances(owner_id,name)
                   VALUES($1,'Основной'),($1,'Соседний') RETURNING id""", OWNER)
            project, other = (row["id"] for row in projects)
            await pool.execute(
                """INSERT INTO network_nodes(instance_id,node_type,ref_id)
                   SELECT $1, CASE WHEN n=$3 THEN 'group' ELSE 'channel' END, $2::bigint+n
                     FROM generate_series(1,$3::int) n""", project, CHANNEL, profile_count)
            await pool.execute(
                "INSERT INTO network_nodes(instance_id,node_type,ref_id) VALUES($1,'gigagroup',$2)",
                other, last)
            headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
            path = f"/api/miniapp/operations/{op_id}/cleanup"
            app = web.Application()
            mini_app_operations.setup_routes(app, pool)

            async with TestClient(TestServer(app)) as client:
                async def timed(method, **kwargs):
                    started = perf_counter()
                    response = await client.request(method, path, headers=headers, **kwargs)
                    body = await response.json()
                    elapsed = (perf_counter() - started) * 1000
                    assert response.status == 200, body
                    return body, elapsed

                report, _ = await timed("GET")
                assert len(report["findings"]) == profile_count
                assert {item["status"] for item in report["findings"]} == set(statuses)
                final = report["findings"][-1]
                assert (final["kind"], final["id"], final["status"]) == (
                    "group", str(last), findings[-1]["status"])
                assert {item["id"] for item in final["projects"]} == {project, other}
                targets = [{"kind": item["kind"], "id": item["id"]} for item in report["findings"]]
                payload = {"action": "detach", "project_id": project, "targets": targets}
                get_times, preview_times = [], []
                for _ in range(25):
                    _, reading = await timed("GET")
                    preview, planning = await timed("POST", json={**payload, "preview": True})
                    get_times.append(reading)
                    preview_times.append(planning)
                    assert len(preview["items"]) == profile_count
                    assert all(item["outcome"] == "ready" for item in preview["items"])
                applied, apply_ms = await timed("POST", json={**payload, "token": preview["token"]})
                assert applied["count"] == profile_count
                after, _ = await timed("GET")
                assert after["history_has_more"] and len(after["history"]) == 50
                assert await pool.fetchval("SELECT count(*) FROM network_nodes WHERE instance_id=$1", project) == 0
                assert await pool.fetchval(
                    "SELECT ref_id FROM network_nodes WHERE instance_id=$1", other) == last
                assert await pool.fetchval(
                    "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2", OWNER, op_id,
                ) == profile_count

                group = [{"kind": "group", "id": str(last)}]
                for action in ({"action": "detach", "project_id": other}, {"action": "disable"}):
                    request = {**action, "targets": group}
                    preview, _ = await timed("POST", json={**request, "preview": True})
                    result, _ = await timed("POST", json={**request, "token": preview["token"]})
                    assert result["count"] == 1
                archive, _ = await timed("GET")
                assert archive["findings"][-1]["kind"] == "group"
                assert archive["findings"][-1]["can_restore"]
                assert await db.restore_removed_channel(pool, OWNER, last)
                restored, _ = await timed("GET")
                assert restored["findings"][-1]["can_disable"]
                assert restored["history"][0]["action"] == "restore"

            def percentiles(values):
                ordered = sorted(values)
                return tuple(round(ordered[math.ceil(len(ordered) * p) - 1], 1) for p in (0.5, 0.95))

            print(f"profiles={profile_count} n=25 get_p50_p95_ms={percentiles(get_times)} "
                  f"preview_p50_p95_ms={percentiles(preview_times)} apply_ms={apply_ms:.1f}")
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1 AND id=ANY($2::bigint[])",
                               OWNER, account_ids)

    asyncio.run(_run(scenario))
