"""Каталог и изменение топологии по подписанному HTTP на PostgreSQL."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from services import mini_app_api, op_worker
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark

__all__ = ["pytestmark"]


def test_project_catalog_links_exact_resource_and_preserves_other_project(monkeypatch):
    secret = "123456:synthetic-project-catalog"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
    submit = AsyncMock(return_value=776)
    monkeypatch.setattr("services.operation_bus.submit", submit)

    async def scenario(pool, op_id):
        project = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Проект') RETURNING id", OWNER)
        other_project = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Другая сеть') RETURNING id", OWNER)
        foreign = CHANNEL + 1
        await pool.execute(
            """INSERT INTO managed_channels(owner_id,acc_id,channel_id,title,type)
               VALUES($1,111,$2,'Чужой канал','channel')""", OWNER + 1, foreign)
        try:
            app = web.Application()
            mini_app_api.setup_routes(app, pool)
            base = "/api/miniapp/networks"
            path = f"{base}/{project}"
            owner = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
            outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
            async with TestClient(TestServer(app)) as client:
                catalog = path + "/catalog?type=channel&q=" + str(CHANNEL)
                assert (await client.get(catalog)).status == 401
                assert (await client.get(catalog, headers=outsider)).status == 404
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
                assert (await client.get(catalog, headers=owner)).status == 403
                projects = await (await client.get(base, headers=owner)).json()
                assert projects["resource_menu"] is True
                assert {item["id"] for item in projects["networks"]} >= {project, other_project}
                for endpoint, key, field in (("channels", "channels", "channel_id"), ("bots", "bots", "bot_id")):
                    response = await client.get(f"/api/miniapp/{endpoint}", headers=owner)
                    assert response.status == 200, await response.text()
                    payload = await response.json()
                    assert payload["resource_menu"] is True and payload[key]
                    assert all(item[field + "_text"] == str(item[field]) for item in payload[key])
                    if endpoint == "channels":
                        assert str(CHANNEL) in {item[field + "_text"] for item in payload[key]}
                        assert str(foreign) not in {item[field + "_text"] for item in payload[key]}
                    else:
                        assert all(item["is_owned"] for item in payload[key])
                add = {"type": "channel", "object_id": str(CHANNEL), "label": "Новости"}
                for bad in (True, 1.2, "bad", str(2**63)):
                    assert (await client.post(path + "/nodes", headers=owner,
                                              json={**add, "object_id": bad})).status == 400
                assert (await client.post(path + "/nodes", headers=owner,
                                          json={**add, "object_id": str(foreign)})).status == 404
                assert (await client.post(path + "/nodes", headers=owner,
                                          json={**add, "type": "group"})).status == 404
                assert (await client.post(path + "/nodes", headers=outsider, json=add)).status == 404

                responses = await asyncio.gather(*(client.post(path + "/nodes", headers=owner, json=add)
                                                   for _ in range(2)))
                links = [await response.json() for response in responses]
                node = links[0]["id"]
                assert links[1]["id"] == node
                assert sum(bool(link.get("already_linked")) for link in links) == 1
                second = await (await client.post(f"{base}/{other_project}/nodes", headers=owner,
                                                  json=add)).json()
                assert second["id"] != node
                unbound = await (await client.post(path + "/nodes", headers=owner,
                    json={"type": "channel", "label": "Шаблон"})).json()
                assert (await client.post(path + "/edges", headers=owner,
                    json={"from_id": node, "to_id": second["id"]})).status == 404
                edge = await client.post(path + "/edges", headers=owner,
                    json={"from_id": node, "to_id": unbound["id"]})
                assert edge.status == 200, await edge.text()
                edge_id = (await edge.json())["id"]
                again = await (await client.post(path + "/edges", headers=owner,
                    json={"from_id": node, "to_id": unbound["id"]})).json()
                assert again == {"ok": True, "id": edge_id, "already_linked": True}

                detail = await (await client.get(path, headers=owner)).json()
                channel = next(item for item in detail["nodes"] if item["id"] == node)
                assert detail["project_pilot"] is False
                assert channel["object_id_text"] == str(CHANNEL) and channel["resource_available"]
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
                response = await client.get(catalog, headers=owner)
                assert (await response.json())["items"] == [
                    {"id": str(CHANNEL), "label": "Канал", "username": ""}]
                assert (await (await client.get(
                    path + "/catalog?type=channel&q=" + str(foreign), headers=owner)).json())["items"] == []
                targets_path = f"/api/miniapp/mass_publish/targets?project_id={project}&project_channel_id={CHANNEL}"
                preview = await (await client.get(targets_path, headers=owner)).json()
                assert preview["sample"][0]["channel_id_text"] == str(CHANNEL)
                payload = {"project_id": project, "project_channel_id": str(CHANNEL),
                           "channel_ids": [str(CHANNEL)], "text": "Синтетическая публикация"}
                queued = await client.post("/api/miniapp/mass_publish", headers=owner, json=payload)
                assert queued.status == 200, await queued.text()
                assert submit.await_args.args[3]["channel_ids"] == [CHANNEL]

                cleanup_path = f"/api/miniapp/operations/{op_id}/cleanup"
                cleanup = {"action": "detach", "targets": [CHANNEL_TARGET], "project_id": project}
                report_preview = await client.post(cleanup_path, headers=owner, json={**cleanup, "preview": True})
                assert report_preview.status == 200, await report_preview.text()
                confirmation = {**cleanup, "token": (await report_preview.json())["token"]}

                detach = f"{base}/nodes/{node}"
                assert (await client.delete(detach, headers=outsider)).status == 404
                deployment = await pool.fetchval(
                    """INSERT INTO operation_queue(owner_id,op_type,status,params)
                       VALUES($1,'deploy_network','running',jsonb_build_object('instance_id',$2::int))
                       RETURNING id""", OWNER, project)
                assert (await client.delete(detach, headers=owner)).status == 409
                assert (await client.post(cleanup_path, headers=owner, json=confirmation)).status == 409
                blocked = await client.post(cleanup_path, headers=owner, json={**cleanup, "preview": True})
                assert "развёртывания" in (await blocked.json())["items"][0]["reason"]
                await pool.execute("DELETE FROM operation_queue WHERE id=$1", deployment)
                running = await pool.fetchval(
                    """INSERT INTO operation_queue(owner_id,op_type,status,params)
                       VALUES($1,'mass_publish','running',jsonb_build_object('project_id',$2::int))
                       RETURNING id""", OWNER, project)
                assert (await client.delete(detach, headers=owner)).status == 409
                await pool.execute("UPDATE operation_queue SET status='pending' WHERE id=$1", running)
                assert (await client.delete(detach, headers=owner)).status == 200
                assert (await client.delete(detach, headers=owner)).status == 404
                assert (await client.post(cleanup_path, headers=owner, json=confirmation)).status == 409
                assert not await pool.fetchval(
                    "SELECT 1 FROM operation_audit WHERE owner_id=$1 AND operation_id=$2", OWNER, op_id)
                assert (await (await client.get(path, headers=owner)).json())["edges"] == []
                assert (await (await client.get(targets_path, headers=owner)).json())["total"] == 0
                assert (await client.post("/api/miniapp/mass_publish", headers=owner, json=payload)).status == 409
                stopped = await op_worker._exec_mass_publish(pool, None, running, OWNER, submit.await_args.args[3])
                assert stopped["status"] == "failed"
                assert "Каналы больше не доступны" in stopped["summary"]
                assert await pool.fetchval("SELECT 1 FROM network_nodes WHERE id=$1", second["id"])
                assert await pool.fetchval("SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                                           OWNER, CHANNEL)
        finally:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER + 1, foreign)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("changed", ["detached", "revoked", "invalid_targets", "new_target"])
def test_queued_project_publish_rechecks_scope_before_preparing_post(monkeypatch, changed):
    from database import db
    from services import account_manager, editorial_review

    secret = "123456:synthetic-project-queue"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "circuit_breaker_status", AsyncMock(return_value={}))
    monkeypatch.setattr(db, "notify_if_enabled", AsyncMock())
    editor = AsyncMock(return_value=(False, []))
    send = AsyncMock(return_value={"message_id": 1})
    monkeypatch.setattr(editorial_review, "autonomous_block", editor)
    monkeypatch.setattr(account_manager, "post_to_channel", send)

    async def scenario(pool, check_id):
        projects = await pool.fetch(
            """INSERT INTO network_instances(owner_id,name)
               VALUES($1,'Публикации'),($1,'Соседний проект') RETURNING id""", OWNER)
        project, other = (row["id"] for row in projects)
        await pool.execute(
            """INSERT INTO network_nodes(instance_id,node_type,ref_id)
               VALUES($1,'channel',$3),($2,'channel',$3)""", project, other, CHANNEL)
        app = web.Application()
        mini_app_api.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

        async with TestClient(TestServer(app)) as client:
            endpoint = "/api/miniapp/mass_publish"
            preview_path = endpoint + "/targets"
            for bad_query in (
                f"?project_channel_id={CHANNEL}",
                f"?project_id={project}&project_id={other}",
                f"?project_id={project}&project_channel_id={CHANNEL}&project_channel_id=1",
            ):
                assert (await client.get(preview_path + bad_query, headers=headers)).status == 400
            preview = await client.get(
                f"{preview_path}?project_id={project}&project_channel_id={CHANNEL}", headers=headers)
            assert (await preview.json())["sample"][0]["channel_id_text"] == str(CHANNEL)
            payload = {"project_id": project, "project_channel_id": str(CHANNEL),
                       "channel_ids": [str(CHANNEL)], "text": "Синтетический пост",
                       "scheduled_for": tomorrow.isoformat()}
            for invalid in (True, float(project) + .5):
                bad = await client.post(endpoint, headers=headers, json={**payload, "project_id": invalid})
                assert bad.status == 400
            first = await client.post(endpoint, headers=headers, json=payload)
            assert first.status == 200, await first.text()
            op_id = (await first.json())["op_id"]
            repeated = await client.post(endpoint, headers=headers, json=payload)
            assert repeated.status == 200 and (await repeated.json())["op_id"] == op_id
            queued = await pool.fetchrow(
                "SELECT params, status, scheduled_for FROM operation_queue WHERE id=$1", op_id)
            params = json.loads(queued["params"])
            assert (params["project_id"], params["channel_ids"], queued["status"]) == (project, [CHANNEL], "pending")
            assert queued["scheduled_for"] == tomorrow

            if changed == "detached":
                path = f"/api/miniapp/operations/{check_id}/cleanup"
                cleanup = {"action": "detach", "project_id": project, "targets": [CHANNEL_TARGET]}
                review = await client.post(path, headers=headers, json={**cleanup, "preview": True})
                assert review.status == 200, await review.text()
                applied = await client.post(
                    path, headers=headers, json={**cleanup, "token": (await review.json())["token"]})
                assert applied.status == 200 and (await applied.json())["count"] == 1
                expected = "Каналы больше не доступны в проекте"
            elif changed == "revoked":
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "")
                expected = "Доступ к публикациям проекта отозван"
            elif changed == "invalid_targets":
                await pool.execute(
                    """UPDATE operation_queue SET params=params || '{"channel_ids":[]}'::jsonb WHERE id=$1""", op_id)
                expected = "Некорректный список каналов проекта"
            else:
                account = await pool.fetchrow(
                    """SELECT * FROM tg_accounts WHERE owner_id=$1 AND phone='synthetic-project-queue'""", OWNER)
                monkeypatch.setattr(op_worker.resource_selector, "select_all_active",
                                    AsyncMock(return_value=[dict(account)]))
                monkeypatch.setattr(op_worker, "_claim_available_accounts",
                                    AsyncMock(return_value=[dict(account)]))
                monkeypatch.setattr(op_worker, "release_accounts", AsyncMock())
                await pool.execute(
                    "UPDATE managed_channels SET acc_id=$1, access_hash=101 WHERE owner_id=$2 AND channel_id=$3",
                    account["id"], OWNER, CHANNEL)
                await pool.execute(
                    """INSERT INTO managed_channels(owner_id,acc_id,channel_id,access_hash,title,type)
                       VALUES($1,$2,$3,102,'Новый канал','channel')""", OWNER, account["id"], CHANNEL + 1)
                await pool.execute(
                    "INSERT INTO network_nodes(instance_id,node_type,ref_id) VALUES($1,'channel',$2)",
                    project, CHANNEL + 1)
                expected = "Опубликовано"

            # Выполняем только выбранную синтетическую строку штатным диспетчером.
            claimed = await pool.fetchrow(
                """UPDATE operation_queue SET status='running', started_at=now() WHERE id=$1
                   RETURNING id, owner_id, op_type, params""", op_id)
            await op_worker._run_op_task(pool, None, dict(claimed))
            state = await pool.fetchrow("SELECT status, result FROM operation_queue WHERE id=$1", op_id)
            assert state["status"] == ("done" if changed == "new_target" else "failed")
            result = json.loads(state["result"])
            if changed == "new_target":
                send.assert_awaited_once()
                assert send.await_args.args[1] == CHANNEL
                assert result["ok"] == 1
                editor.assert_awaited_once()
                assert await pool.fetchval(
                    "SELECT target FROM operation_log WHERE op_id=$1 AND status='ok'", op_id) == str(CHANNEL)
            else:
                assert expected in result["summary"]
                editor.assert_not_awaited()
                send.assert_not_awaited()
            card = await client.get(f"/api/miniapp/operation/{op_id}", headers=headers)
            assert card.status == 200
            assert expected in await card.text()
            assert await pool.fetchval(
                "SELECT 1 FROM network_nodes WHERE instance_id=$1 AND ref_id=$2", other, CHANNEL)

    async def with_synthetic_account(pool, check_id):
        if changed != "new_target":
            await scenario(pool, check_id)
            return
        account_id = await pool.fetchval(
            """INSERT INTO tg_accounts(owner_id,phone,session_str)
               VALUES($1,'synthetic-project-queue','synthetic-session') RETURNING id""", OWNER)
        try:
            await scenario(pool, check_id)
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2", account_id, OWNER)

    asyncio.run(_run(with_synthetic_account))
