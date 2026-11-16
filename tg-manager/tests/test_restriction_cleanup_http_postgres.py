"""Подписанная сессия Mini App и точное подтверждение на тестовом PostgreSQL."""
import asyncio
import json
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from services import account_manager, mini_app_api, mini_app_operations, op_worker, restriction_cleanup
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import (
    BOT, BOT_TARGET, CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark,
)

__all__ = ["pytestmark"]


def test_context_report_reads_exact_target_without_widening(monkeypatch):
    secret = "123456:synthetic-context-cleanup"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, op_id):
        app = web.Application()
        mini_app_api.setup_routes(app, pool)
        app.on_startup.clear()
        foreign_op = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id,op_type,status,result,finished_at)
               VALUES($1,'check_owned_restrictions','done','{}',now()) RETURNING id""", OWNER + 1,
        )
        owner = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        foreign = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        query = f"kind=channel&resource_id={CHANNEL}"
        try:
            async with TestClient(TestServer(app)) as client:
                await run_context_checks(client, owner, foreign, path, query, pool, op_id, foreign_op)
        finally:
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1 AND id=$2", OWNER + 1, foreign_op)

    async def run_context_checks(client, owner, foreign, path, query, pool, op_id, foreign_op):
        summary = "/api/miniapp/restrictions/dead?summary=1"
        assert (await client.get(summary)).status == 401
        for headers, expected in ((owner, op_id), (foreign, foreign_op)):
            response = await client.get(summary, headers=headers)
            assert response.status == 200
            assert await response.json() == {"ok": True, "op_id": str(expected), "cleanup_available": True}
        ordinary = await (await client.get("/api/miniapp/restrictions/dead", headers=owner)).json()
        assert ordinary["op_id"] == str(op_id) and ordinary["dead"] == []
        request = await client.get(f"{path}?{query}", headers=owner)
        assert request.status == 200
        scoped = await request.json()
        assert scoped["target"] == CHANNEL_TARGET
        assert [(item["kind"], item["id"], item["status"]) for item in scoped["findings"]] == [
            ("channel", str(CHANNEL), "check_failed"),
        ]
        assert scoped["history"] == [] and "receipts" not in scoped
        for target in (f"kind=group&resource_id={CHANNEL}", f"kind=channel&resource_id={CHANNEL+1}"):
            empty = await client.get(f"{path}?{target}", headers=owner)
            assert empty.status == 200 and not (await empty.json())["findings"]
        for target in ("kind=channel", query + "&kind=bot", query + "&receipt=abc", "kind=channel&resource_id=0"):
            assert (await client.get(f"{path}?{target}", headers=owner)).status == 400
        assert (await client.get(f"{path}?{query}", headers=foreign)).status == 404
        full = await (await client.get(path, headers=owner)).json()
        assert len(full["findings"]) == 3 and full["history"] == []
        body = {"action": "disable", "targets": [CHANNEL_TARGET]}
        assert (await client.post(f"{path}?{query}", headers=owner, json=body)).status == 400
        preview = await (await client.post(path, headers=owner, json={**body, "preview": True})).json()
        token = preview["token"]
        assert (await client.post(path, headers=owner, json={
            **body, "token": token, "targets": [BOT_TARGET],
        })).status == 409
        applied = await client.post(path, headers=owner, json={**body, "token": token})
        assert applied.status == 200 and (await applied.json())["count"] == 1
        await pool.execute(
            """INSERT INTO operation_audit(owner_id,operation_id,action,target,result)
               VALUES($1,$2,'restriction_cleanup_disable',$3,'success'),
                     ($1,$2,'bot_restore',$4,'success')""", OWNER, op_id, f"bot:{BOT}", str(CHANNEL),
        )
        reread = await (await client.get(f"{path}?{query}", headers=owner)).json()
        assert reread["findings"][0]["can_restore"]
        assert [entry["target"] for entry in reread["history"]] == [f"channel:{CHANNEL}"]
        assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1 AND added_by=$2", BOT, OWNER)
        monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
        assert (await client.get(f"{path}?{query}", headers=owner)).status == 403

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("change", ["disable", "reassign", "restored", "reimported"])
def test_queued_channel_edit_revalidates_targets_after_cleanup(monkeypatch, change):
    from database import db as channel_db
    from services import mini_app_channels as channel_api

    secret = "123456:synthetic-cleanup-authentication-only"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "circuit_breaker_status", AsyncMock(return_value={}))
    monkeypatch.setattr(op_worker, "_circuit_breaker_record", AsyncMock())
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
        foreign_audit = await pool.fetchval(
            """INSERT INTO operation_audit(owner_id,action,target,result)
               VALUES($1,'channel_restore',$2,'success') RETURNING id""", OWNER + 1, str(other))
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        channel_api.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{check_id}/cleanup"
        action = {"action": "disable", "targets": [CHANNEL_TARGET]}
        try:
            async with TestClient(TestServer(app)) as client:
                preview = await client.post(path, headers=headers, json={**action, "preview": True})
                assert preview.status == 200, await preview.text()
                confirmation = {**action, "token": (await preview.json())["token"]}
                if change in ("disable", "restored", "reimported"):
                    applied = await client.post(path, headers=headers, json=confirmation)
                    assert applied.status == 200 and (await applied.json())["count"] == 1
                    report = await (await client.get(path, headers=headers)).json()
                    item = next(item for item in report["findings"] if item["id"] == str(CHANNEL))
                    assert item["status"] == "check_failed" and item["can_restore"]
                    if change == "restored":
                        assert await channel_db.restore_removed_channel(pool, OWNER, CHANNEL)
                    elif change == "reimported":
                        assert await channel_db.add_managed_channels(
                            pool, OWNER, 992660, [{"id": CHANNEL, "title": "Подключён вновь", "type": "channel"}],
                            reconnect=True)
                        assert not await pool.fetchval(
                            """SELECT 1 FROM operation_audit WHERE owner_id=$1
                                 AND target=$2 AND action='channel_restore'""", OWNER, str(CHANNEL))
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
                if change in ("restored", "reimported"):
                    skip = next(row for row in journal["logs"] if row["target"] == str(CHANNEL))
                    assert "Подтвердите новую команду" in skip["message"]
                    retry_params = {**params, "retry_of_op": op_id}
                    retry_id = await pool.fetchval(
                        """INSERT INTO operation_queue(owner_id,op_type,status,params)
                           VALUES($1,'bulk_chan_exec','pending',$2::jsonb) RETURNING id""",
                        OWNER, json.dumps(retry_params))
                    retried = await op_worker._exec_bulk_chan_exec(pool, None, retry_id, OWNER, retry_params)
                    assert (retried["ok"], retried["fail"]) == (1, 2)
                    editor.assert_awaited_once()

                    fresh = await client.post(f"/api/miniapp/channel/{CHANNEL}/edit", headers=headers,
                                              json={"op":"about", "value":"Новое подтверждение"})
                    assert fresh.status == 200, await fresh.text()
                    fresh_id = (await fresh.json())["op_id"]
                    fresh_params = json.loads(await pool.fetchval(
                        "SELECT params FROM operation_queue WHERE id=$1", fresh_id))
                    fresh_result = await op_worker._exec_bulk_chan_exec(pool, None, fresh_id, OWNER, fresh_params)
                    assert fresh_result["ok"] == 1
                    assert editor.await_args.args[1] == CHANNEL
        finally:
            await pool.execute(
                "DELETE FROM resource_selection_invalidations WHERE owner_id=$1 AND scope=$2",
                OWNER + 1, f"channel:{other}",
            )
            await pool.execute("DELETE FROM operation_audit WHERE owner_id=$1 AND id=$2", OWNER + 1, foreign_audit)
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


@pytest.mark.parametrize("change", ["disabled", "foreign", "deleted", "empty_session", "wrong_link_kind"])
def test_disable_rechecks_recoverable_catalog_binding(monkeypatch, change):
    secret = "123456:synthetic-disable-binding"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    account = 992660

    async def scenario(pool, op_id):
        from database import db

        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        action = {"action": "disable", "targets": [CHANNEL_TARGET]}
        project = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Локальный проект') RETURNING id", OWNER,
        )
        async with TestClient(TestServer(app)) as client:
            first = await (await client.post(path, json={**action, "preview": True}, headers=headers)).json()
            assert first["token"]
            row_id = await pool.fetchval(
                "SELECT id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
            )
            try:
                if change == "wrong_link_kind":
                    await pool.execute(
                        "INSERT INTO network_nodes(instance_id,node_type,ref_id) VALUES($1,'group',$2)",
                        project, CHANNEL,
                    )
                elif change == "deleted":
                    await pool.execute("DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2", account, OWNER)
                else:
                    await pool.execute(
                        """UPDATE tg_accounts SET owner_id=$2, is_active=$3, session_str=$4
                           WHERE id=$1 AND owner_id=$5""",
                        account, OWNER + (change == "foreign"), change != "disabled",
                        "" if change == "empty_session" else "synthetic-reassigned-session", OWNER,
                    )
                rejected = await client.post(path, json={**action, "token": first["token"]}, headers=headers)
                assert rejected.status == 409, await rejected.text()
                report = await (await client.get(path, headers=headers)).json()
                finding = next(item for item in report["findings"] if item["id"] == str(CHANNEL))
                assert finding["status"] == "check_failed" and finding["active"] and not finding["can_disable"]
                reason = ("связь с другим типом" if change == "wrong_link_kind" else "недоступен для восстановления")
                assert reason in finding["disable_reason"]
                if change == "wrong_link_kind":
                    assert finding["projects"] == []
                    assert finding["conflicting_projects"] == [{"id": project, "name": "Локальный проект"}]
                assert "synthetic-reassigned-session" not in str(report)
                assert not report["history"] and not report["receipts"]
                blocked = await (await client.post(path, json={**action, "preview": True}, headers=headers)).json()
                assert blocked["token"] is None
                assert blocked["items"][0]["outcome"] == "skipped" and reason in blocked["items"][0]["reason"]
                if change == "empty_session":
                    mixed = {**action, "targets": [CHANNEL_TARGET, BOT_TARGET]}
                    plan = await (await client.post(path, json={**mixed, "preview": True}, headers=headers)).json()
                    applied = await client.post(path, json={**mixed, "token": plan["token"]}, headers=headers)
                    assert applied.status == 200
                    result = await applied.json()
                    assert [(item["kind"], item["outcome"]) for item in result["items"]] == [
                        ("bot", "applied"), ("channel", "skipped"),
                    ]
                assert await pool.fetchval(
                    "SELECT 1 FROM managed_channels WHERE owner_id=$1 AND id=$2", OWNER, row_id,
                )
                if change != "wrong_link_kind":
                    await pool.execute(
                        "INSERT INTO network_nodes(instance_id,node_type,ref_id) VALUES($1,'channel',$2)", project, CHANNEL,
                    )
                    detach = {**action, "action": "detach", "project_id": project}
                    plan = await (await client.post(path, json={**detach, "preview": True}, headers=headers)).json()
                    detached = await client.post(path, json={**detach, "token": plan["token"]}, headers=headers)
                    assert detached.status == 200 and (await detached.json())["count"] == 1
            finally:
                await pool.execute("DELETE FROM network_nodes WHERE instance_id=$1", project)
                await pool.execute(
                    """INSERT INTO tg_accounts(id, owner_id, phone, session_str)
                       VALUES($1,$2,'synthetic-cleanup','synthetic-restore-session')
                       ON CONFLICT(id) DO UPDATE SET owner_id=EXCLUDED.owner_id, is_active=TRUE,
                                                     session_str=EXCLUDED.session_str""", account, OWNER,
                )
            fresh = await (await client.post(path, json={**action, "preview": True}, headers=headers)).json()
            applied = await client.post(path, json={**action, "token": fresh["token"]}, headers=headers)
            assert applied.status == 200 and (await applied.json())["count"] == 1
            assert await db.restore_removed_channel(pool, OWNER, CHANNEL)
            assert await pool.fetchval(
                "SELECT id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
            ) == row_id

    asyncio.run(_run(scenario))


def test_disable_holds_recovery_account_through_archive(monkeypatch):
    import asyncpg
    from database import db

    async def scenario(pool, op_id):
        archive = db.remove_managed_channels_on_connection

        async def attempt_account_change(*args, **kwargs):
            async with pool.acquire() as other:
                with pytest.raises(asyncpg.LockNotAvailableError):
                    async with other.transaction():
                        await other.execute("SET LOCAL lock_timeout = '100ms'")
                        await other.execute(
                            "UPDATE tg_accounts SET is_active=FALSE WHERE id=992660 AND owner_id=$1", OWNER,
                        )
            return await archive(*args, **kwargs)

        preview = await restriction_cleanup.plan(pool, OWNER, op_id, [CHANNEL_TARGET], "disable")
        monkeypatch.setattr(db, "remove_managed_channels_on_connection", attempt_account_change)

        def verify(plan):
            assert plan == preview

        result = await restriction_cleanup.apply(
            pool, OWNER, op_id, [CHANNEL_TARGET], "disable", verify=verify,
        )
        assert result["count"] == 1
        assert await db.restore_removed_channel(pool, OWNER, CHANNEL)

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

                await pool.execute(
                    """INSERT INTO va_channel_admin(owner_id,channel_id,enabled)
                       VALUES($1,$2,TRUE),($1,$3,TRUE)""", OWNER, CHANNEL, last)
                request = {"action": "disable", "targets": targets}
                preview, disable_preview_ms = await timed("POST", json={**request, "preview": True})
                assert all(item["outcome"] == "ready" for item in preview["items"])
                result, disable_ms = await timed("POST", json={**request, "token": preview["token"]})
                assert result["count"] == profile_count
                assert [(item["id"], item["status"]) for item in result["items"]] == [
                    (str(item["id"]), item["status"]) for item in findings
                ]
                assert await pool.fetchval(
                    """SELECT count(*) FROM removed_channels WHERE owner_id=$1
                       AND channel_id BETWEEN $2 AND $3 AND reason=$4""",
                    OWNER, CHANNEL + 1, last, db.REMOVED_BY_OWNER,
                ) == profile_count
                assert not await pool.fetchval(
                    """SELECT EXISTS(SELECT FROM managed_channels WHERE owner_id=$1
                       AND channel_id BETWEEN $2 AND $3)""", OWNER, CHANNEL + 1, last,
                )
                assert await pool.fetchval(
                    """SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2
                       AND action='restriction_cleanup_disable'""", OWNER, op_id,
                ) == profile_count + 1
                assert await pool.fetchval(
                    "SELECT enabled FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
                )
                assert not await pool.fetchval(
                    "SELECT enabled FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2", OWNER, last,
                )
                assert await db.restore_removed_channel(pool, OWNER, last)
                assert await pool.fetchval(
                    "SELECT type FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, last,
                ) == "gigagroup"

            def percentiles(values):
                ordered = sorted(values)
                return tuple(round(ordered[math.ceil(len(ordered) * p) - 1], 1) for p in (0.5, 0.95))

            print(f"profiles={profile_count} n=25 get_p50_p95_ms={percentiles(get_times)} "
                  f"preview_p50_p95_ms={percentiles(preview_times)} apply_ms={apply_ms:.1f} "
                  f"disable_preview_ms={disable_preview_ms:.1f} disable_ms={disable_ms:.1f}")
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1 AND id=ANY($2::bigint[])",
                               OWNER, account_ids)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize(("op_type", "selection", "return_method"), [
    ("mass_publish", "fixed", "disabled"),
    ("mass_publish", "fixed", "restore"),
    ("mass_publish", "dynamic", "reimport"),
    ("quick_post", "fixed", "restore"),
    ("bulk_post_chans", "legacy", "restore"),
])
def test_publication_skips_catalog_changes_after_queueing(monkeypatch, op_type, selection, return_method):
    from database import db as channel_db
    from services import editorial_review, operation_bus
    from services import mini_app_channels as channel_api

    secret = "123456:synthetic-publish-cleanup"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    publish = AsyncMock(return_value={"msg_id": 123})
    monkeypatch.setattr(account_manager, "post_to_channel", publish)
    monkeypatch.setattr(editorial_review, "autonomous_block", AsyncMock(return_value=(False, [])))
    monkeypatch.setattr(op_worker, "_claim_available_accounts", AsyncMock(side_effect=lambda _, rows, __: rows))
    monkeypatch.setattr(op_worker, "try_claim_account", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "_single_account_parked", AsyncMock(return_value=None))
    monkeypatch.setattr(op_worker, "release_accounts", AsyncMock())
    monkeypatch.setattr(op_worker, "_governed_sleep", AsyncMock())
    monkeypatch.setattr(op_worker, "_pause_watching_for_cancel", AsyncMock())
    monkeypatch.setattr(op_worker, "_flood_cooldown_left", lambda _: 0)
    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "circuit_breaker_status", AsyncMock(return_value={}))

    async def scenario(pool, check_id):
        sibling = CHANNEL + 1
        source_row = await pool.fetchval(
            "UPDATE managed_channels SET access_hash=111 WHERE owner_id=$1 AND channel_id=$2 RETURNING id",
            OWNER, CHANNEL,
        )
        sibling_row = await pool.fetchval(
            """INSERT INTO managed_channels(owner_id,acc_id,channel_id,access_hash,title,type)
               VALUES($1,992660,$2,222,'Соседний канал','channel') RETURNING id""",
            OWNER, sibling,
        )
        account = dict(await pool.fetchrow("SELECT * FROM tg_accounts WHERE id=992660"))
        monkeypatch.setattr(op_worker.resource_selector, "select_all_active", AsyncMock(return_value=[account]))
        params = {"text": "Синтетическая публикация", "delay": -1}
        executor = op_worker._exec_mass_publish
        if selection == "fixed":
            params["channel_ids"] = [str(CHANNEL), str(sibling)]
        elif selection == "legacy":
            params.update(acc_id=992660, channel_ids=[source_row, str(sibling_row)])
            executor = op_worker._exec_bulk_post_chans

        async def enqueue(payload):
            return await pool.fetchval(
                """INSERT INTO operation_queue(owner_id,op_type,status,params)
                   VALUES($1,$2,'running',$3::jsonb) RETURNING id""",
                OWNER, op_type, json.dumps(payload),
            )

        op_id = await enqueue(params)
        before = await pool.fetchval("SELECT created_at FROM operation_queue WHERE id=$1", op_id)
        foreign_audit = await pool.fetchval(
            """INSERT INTO operation_audit(owner_id,action,target,result)
               VALUES($1,'channel_restore',$2,'success') RETURNING id""", OWNER + 1, str(sibling),
        )
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        channel_api.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{check_id}/cleanup"
        action = {"action": "disable", "targets": [CHANNEL_TARGET]}
        try:
            async with TestClient(TestServer(app)) as client:
                busy = await client.post(path, headers=headers, json={**action, "preview": True})
                busy_plan = await busy.json()
                assert busy.status == 200 and busy_plan["token"] is None
                assert "текущей публикации" in busy_plan["items"][0]["reason"]
                await pool.execute("UPDATE operation_queue SET status='pending' WHERE id=$1", op_id)

                review = await client.post(path, headers=headers, json={**action, "preview": True})
                assert review.status == 200, await review.text()
                plan = await review.json()
                confirmation = {**action, "token": plan["token"]}
                applied = await client.post(path, headers=headers, json=confirmation)
                assert applied.status == 200 and (await applied.json())["count"] == 1
                restored_path = f"/api/miniapp/channel/{CHANNEL}/restore"
                if return_method == "restore":
                    outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
                    assert (await client.post(restored_path, headers=outsider)).status == 404
                    restored = await client.post(restored_path, headers=headers)
                    assert restored.status == 200, await restored.text()
                elif return_method == "reimport":
                    resources = [{"id": CHANNEL, "title": "Возвращён", "type": "channel", "access_hash": 111}]
                    assert await channel_db.add_managed_channels(pool, OWNER, 992660, resources) == 0
                    before = await pool.fetchval(
                        "UPDATE operation_queue SET created_at=clock_timestamp() WHERE id=$1 RETURNING created_at",
                        op_id,
                    )
                    assert await channel_db.add_managed_channels(pool, OWNER, 992660, resources, reconnect=True) == 1
                    report = await (await client.get(path, headers=headers)).json()
                    assert any(row["action"] == "reconnect" and row["target"] == str(CHANNEL)
                               for row in report["history"])
                    await pool.execute("UPDATE managed_channels SET access_hash=111 WHERE owner_id=$1 AND channel_id=$2",
                                       OWNER, CHANNEL)

                from services.db_maintenance import _prune_batched

                await pool.execute(
                    """UPDATE operation_audit SET occurred_at=now()-interval '61 days'
                       WHERE owner_id=$1 AND action=ANY($2::text[])""",
                    OWNER, ["channel_restore", "channel_reconnect", "restriction_cleanup_disable"],
                )
                pruned, error = await _prune_batched(
                    pool, "operation_audit", "owner_id=$1 AND occurred_at < now()-interval '60 days'", OWNER,
                )
                assert error is None and pruned == (1 if return_method == "disabled" else 2)
                await pool.execute("UPDATE operation_queue SET status='running' WHERE id=$1", op_id)
                result = await executor(pool, None, op_id, OWNER, params)
                assert result["ok"] == 1 and result.get("failed", result.get("fail")) == 1
                publish.assert_awaited_once()
                assert publish.await_args.args[1] == sibling
                log_response = await client.get(f"/api/miniapp/operation/{op_id}/log", headers=headers)
                assert log_response.status == 200, await log_response.text()
                logs = (await log_response.json())["logs"]
                skipped, = [entry for entry in logs if entry["status"] == "skipped"]
                assert skipped["target"] == str(CHANNEL)
                assert "Подтвердите новую публикацию" in skipped["message"]
                outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
                assert (await client.get(f"/api/miniapp/operation/{op_id}/log", headers=outsider)).status == 404
                exported = await client.get(f"/api/miniapp/operation/{op_id}/log.csv", headers=headers)
                assert exported.status == 200
                csv = await exported.text()
                assert str(CHANNEL) in csv and "synthetic-restore-session" not in csv

                for _ in range(2):
                    await executor(pool, None, op_id, OWNER, params)
                assert await pool.fetchval(
                    "SELECT count(*) FROM operation_log WHERE op_id=$1 AND status='skipped'", op_id) == 1
                retry = {**params, "retry_of_op": op_id}
                retried = await executor(pool, None, await enqueue(retry), OWNER, retry)
                assert retried["ok"] == 1
                publish.assert_awaited_once()

                if return_method != "disabled":
                    await pool.execute("UPDATE operation_queue SET status='partial' WHERE id=$1", op_id)
                    await op_worker._reschedule_recurring(
                        pool, None, op_id, OWNER, op_type, {**params, "repeat_interval_min": 15},
                        productive=True,
                    )
                    next_id = await pool.fetchval(
                        "SELECT params->>'recurring_next_op' FROM operation_queue WHERE id=$1", op_id)
                    assert next_id
                    next_row = await pool.fetchrow(
                        "SELECT params, scheduled_for > now() AS future FROM operation_queue WHERE id=$1",
                        int(next_id),
                    )
                    following = json.loads(next_row["params"])
                    assert next_row["future"] and following["_publication_catalog_since"] == before.isoformat()
                    await pool.execute("DELETE FROM operation_queue WHERE id=$1", op_id)
                    await executor(pool, None, int(next_id), OWNER, following)
                    assert [call.args[1] for call in publish.await_args_list] == [sibling, sibling]

                    fresh_id = (await enqueue(params) if selection == "legacy" else
                                await operation_bus.submit(pool, OWNER, op_type, params, dedup_window_sec=0))
                    fresh = await executor(pool, None, fresh_id, OWNER, params)
                    assert fresh["ok"] == 2
                    assert {call.args[1] for call in publish.await_args_list[-2:]} == {CHANNEL, sibling}
        finally:
            await pool.execute(
                "DELETE FROM resource_selection_invalidations WHERE owner_id=$1 AND scope=$2",
                OWNER + 1, f"channel:{sibling}",
            )
            await pool.execute("DELETE FROM operation_audit WHERE owner_id=$1 AND id=$2", OWNER + 1, foreign_audit)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize(("op_type", "fixed"), [
    ("mass_publish", True), ("quick_post", True), ("mass_publish", False), ("bulk_post_chans", True),
])
def test_publication_copies_use_original_catalog_selection(monkeypatch, op_type, fixed):
    from services import operation_bus
    from services import mini_app_channels as channel_api

    secret = "123456:synthetic-catalog-copy"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "circuit_breaker_status", AsyncMock(return_value={}))
    send = AsyncMock()
    monkeypatch.setattr(account_manager, "post_to_channel", send)

    async def scenario(pool, check_id):
        row_id = await pool.fetchval(
            "SELECT id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
        )
        params = {"text": "Прежняя публикация"}
        if fixed:
            params["channel_ids"] = [str(row_id if op_type == "bulk_post_chans" else CHANNEL)]
        if op_type == "bulk_post_chans":
            params["acc_id"] = 992660

        async def enqueue(payload, status):
            return await pool.fetchval(
                """INSERT INTO operation_queue(owner_id,op_type,status,params)
                   VALUES($1,$2,$3,$4::jsonb) RETURNING id""",
                OWNER, op_type, status, json.dumps(payload),
            )

        source_id = await enqueue(params, "done")
        cutoff = await pool.fetchval("SELECT created_at FROM operation_queue WHERE id=$1", source_id)
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        channel_api.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        source = f"/api/miniapp/operation/{source_id}"
        cleanup = f"/api/miniapp/operations/{check_id}/cleanup"
        action = {"action": "disable", "targets": [CHANNEL_TARGET]}

        async with TestClient(TestServer(app)) as client:
            assert (await client.get(source + "/editable", headers=outsider)).status == 404
            assert (await client.post(source + "/clone", headers=outsider, json={})).status == 404
            editable = await client.get(source + "/editable", headers=headers)
            assert editable.status == (200 if fixed else 409), await editable.text()
            copy = await client.post(source + "/clone", headers=headers, json={"changes": {"text": "Копия"}})
            assert copy.status == (200 if fixed else 409), await copy.text()
            copy_id = None
            if fixed:
                copy_id = (await copy.json())["new_id"]
                stored = json.loads(await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", copy_id))
                assert stored["_publication_catalog_since"] == cutoff.isoformat()
                assert stored["channel_ids"] == params["channel_ids"] and stored["text"] == "Копия"
                assert "retry_of_op" not in stored
                await pool.execute("UPDATE operation_queue SET status='done' WHERE id=$1", copy_id)
                parallel = await asyncio.gather(*(
                    operation_bus.submit(pool, OWNER, op_type, {**params, "cloned_from_op": source_id, "text": text})
                    for text in ("Первая параллельная", "Вторая параллельная")
                ), return_exceptions=True)
                assert sum(isinstance(value, int) for value in parallel) == 1
                assert sum(isinstance(value, operation_bus.ProjectPublishReviewRequired) for value in parallel) == 1
                active_id = next(value for value in parallel if isinstance(value, int))
                await pool.execute("UPDATE operation_queue SET status='done' WHERE id=$1", active_id)
                invalid = [
                    {**params, "channel_ids": [CHANNEL + 1], "cloned_from_op": source_id},
                    {**params, "target": "all", "cloned_from_op": source_id},
                    {**params, "cloned_from_op": source_id, "project_id": 100},
                ]
                for changed in invalid:
                    with pytest.raises(operation_bus.ProjectPublishReviewRequired):
                        await operation_bus.submit(pool, OWNER, op_type, changed)
                with pytest.raises(operation_bus.ProjectPublishReviewRequired):
                    await operation_bus.submit(pool, OWNER + 1, op_type, {**params, "cloned_from_op": source_id})
            else:
                # Динамический источник разрешает только цели с ошибкой в своём журнале.
                await pool.execute(
                    "INSERT INTO operation_log(op_id,step_num,target,status) VALUES($1,1,$2,'error')",
                    source_id, str(CHANNEL),
                )
                retry = {**params, "channel_ids": [CHANNEL], "retry_of_op": source_id}
                retry_id = await operation_bus.submit(pool, OWNER, op_type, retry)
                stored = json.loads(await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", retry_id))
                assert stored["_publication_catalog_since"] == cutoff.isoformat()
                with pytest.raises(operation_bus.ProjectPublishReviewRequired):
                    await operation_bus.submit(pool, OWNER, op_type, {**retry, "channel_ids": [CHANNEL + 1]})
                await pool.execute("UPDATE operation_queue SET status='done' WHERE id=$1", retry_id)

            plan = await client.post(cleanup, headers=headers, json={**action, "preview": True})
            assert plan.status == 200, await plan.text()
            applied = await client.post(cleanup, headers=headers, json={**action, "token": (await plan.json())["token"]})
            assert applied.status == 200 and (await applied.json())["count"] == 1
            restored = await client.post(f"/api/miniapp/channel/{CHANNEL}/restore", headers=headers)
            assert restored.status == 200, await restored.text()
            for route_source in (source, f"/api/miniapp/operation/{copy_id}" if copy_id else source):
                for response in (await client.get(route_source + "/editable", headers=headers),
                                 await client.post(route_source + "/clone", headers=headers, json={})):
                    assert response.status == 409, await response.text()
                    assert operation_bus.CATALOG_PUBLISH_REVIEW_MESSAGE in (await response.json())["error"]
            if fixed:
                old_params = {**params, "cloned_from_op": source_id}
                old_copy = await enqueue(old_params, "pending")
                executor = op_worker._exec_bulk_post_chans if op_type == "bulk_post_chans" else op_worker._exec_mass_publish
                refused = await executor(pool, None, old_copy, OWNER, old_params)
                assert refused["status"] == "failed" and refused["summary"] == operation_bus.CATALOG_PUBLISH_REVIEW_MESSAGE
                send.assert_not_awaited()
                if copy_id:
                    await pool.execute("DELETE FROM operation_queue WHERE id=$1", source_id)
                    copied = {**params, "retry_of_op": copy_id}
                    retry_id = await operation_bus.submit(pool, OWNER, op_type, copied)
                    retry_params = json.loads(await pool.fetchval(
                        "SELECT params FROM operation_queue WHERE id=$1", retry_id))
                    assert retry_params["_publication_catalog_since"] == cutoff.isoformat()
                if op_type == "bulk_post_chans":
                    await pool.execute("UPDATE operation_queue SET status='failed' WHERE id=$1", copy_id)
                    await pool.execute(
                        "INSERT INTO operation_log(op_id,step_num,target,status) VALUES($1,1,$2,'error')",
                        copy_id, str(CHANNEL),
                    )
                    assert await operation_bus.collect_failed_targets(pool, copy_id, op_type) == [row_id]

    asyncio.run(_run(scenario))
