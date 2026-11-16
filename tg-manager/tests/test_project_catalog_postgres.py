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
                reverse = await (await client.post(path + "/edges", headers=owner,
                    json={"from_id": unbound["id"], "to_id": node})).json()
                edge_path = f"{base}/edges/{reverse['id']}"
                assert (await client.delete(edge_path, headers=outsider)).status == 404
                assert (await client.delete(edge_path, headers=owner)).status == 200
                assert (await client.delete(edge_path, headers=owner)).status == 404

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
                with monkeypatch.context() as failure:
                    failure.setattr("database.db.record_manual_action",
                                    AsyncMock(side_effect=RuntimeError("Синтетический сбой аудита")))
                    assert (await client.delete(detach, headers=owner)).status == 500
                assert await pool.fetchval("SELECT 1 FROM network_nodes WHERE id=$1", node)
                assert await pool.fetchval("SELECT 1 FROM network_edges WHERE id=$1", edge_id)
                assert (await client.delete(detach, headers=owner)).status == 200
                assert (await client.delete(detach, headers=owner)).status == 404
                assert (await client.post(cleanup_path, headers=owner, json=confirmation)).status == 409
                assert not await pool.fetchval(
                    "SELECT 1 FROM operation_audit WHERE owner_id=$1 AND operation_id=$2", OWNER, op_id)
                assert (await client.get(path, headers=outsider)).status == 404
                final = await (await client.get(path, headers=owner)).json()
                assert final["edges"] == []
                history = final["topology_history"]
                assert [event["label"] for event in history] == [
                    "Узел отвязан от проекта", "Связь удалена", "Связь удалена",
                    "Связь добавлена", "Связь добавлена", "Узел добавлен", "Узел добавлен",
                ]
                assert history[0]["target"] == f"project:{project} node:{node} channel:{CHANNEL}"
                assert history[1]["target"] == f"project:{project} edge:{edge_id} {node} → {unbound['id']}"
                assert all(event["occurred_at"] for event in history)
                nearby = await (await client.get(f"{base}/{other_project}", headers=owner)).json()
                assert len(nearby["topology_history"]) == 1
                assert f"node:{second['id']}" in nearby["topology_history"][0]["target"]
                assert (await (await client.get(targets_path, headers=owner)).json())["total"] == 0
                assert (await client.post("/api/miniapp/mass_publish", headers=owner, json=payload)).status == 409
                stopped = await op_worker._exec_mass_publish(pool, None, running, OWNER, submit.await_args.args[3])
                assert stopped["status"] == "failed"
                assert "Каналы больше не доступны" in stopped["summary"]
                assert await pool.fetchval("SELECT 1 FROM network_nodes WHERE id=$1", second["id"])
                assert await pool.fetchval("SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                                           OWNER, CHANNEL)
                neighbor_cleanup = {**cleanup, "project_id": other_project}
                prepared = await client.post(cleanup_path, headers=owner,
                                             json={**neighbor_cleanup, "preview": True})
                applied = await client.post(cleanup_path, headers=owner, json={
                    **neighbor_cleanup, "token": (await prepared.json())["token"],
                })
                assert applied.status == 200, await applied.text()
                nearby = await (await client.get(f"{base}/{other_project}", headers=owner)).json()
                assert nearby["topology_history"][0]["label"] == "Ресурс отвязан по результатам проверки"
                assert nearby["topology_history"][0]["target"] == f"channel:{CHANNEL} project:{other_project}"
                assert nearby["topology_history"][0]["operation_id"] == str(op_id)
        finally:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER + 1, foreign)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("changed", [
    "detached", "reattached", "manual_reattached", "neighbor_reattached",
    "revoked", "invalid_targets", "new_target",
])
def test_scheduled_project_publish_rechecks_scope_at_poller(monkeypatch, changed):
    from database import db
    from services import account_manager, editorial_review

    productive = changed in ("new_target", "neighbor_reattached")
    secret = "123456:synthetic-project-queue"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "circuit_breaker_status", AsyncMock(return_value={}))
    monkeypatch.setattr(db, "notify_if_enabled", AsyncMock())
    stopped = AsyncMock()
    monkeypatch.setattr(op_worker, "_notify_recurring_stopped", stopped)
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
        client_time = tomorrow.astimezone(timezone(timedelta(hours=5, minutes=45)))

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
                       "scheduled_for": client_time.isoformat(), "repeat_interval_min": 15}
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

            # Не исполняем другие сугубо тестовые задания, оставленные общей базой.
            excluded = {row["id"] for row in await pool.fetch(
                "SELECT id FROM operation_queue WHERE status='pending' AND id<>$1", op_id)}
            monkeypatch.setattr(op_worker, "_active_op_ids", excluded)
            monkeypatch.setattr(op_worker, "_active_op_tasks", set())
            monkeypatch.setattr(op_worker, "_MAX_PARALLEL", len(excluded) + 2)
            await op_worker._process_pending(pool, None)
            assert not op_worker._active_op_tasks
            assert await pool.fetchval("SELECT status FROM operation_queue WHERE id=$1", op_id) == "pending"

            if changed in ("detached", "reattached", "neighbor_reattached"):
                changed_project = other if changed == "neighbor_reattached" else project
                path = f"/api/miniapp/operations/{check_id}/cleanup"
                cleanup = {"action": "detach", "project_id": changed_project, "targets": [CHANNEL_TARGET]}
                review = await client.post(path, headers=headers, json={**cleanup, "preview": True})
                assert review.status == 200, await review.text()
                applied = await client.post(
                    path, headers=headers, json={**cleanup, "token": (await review.json())["token"]})
                assert applied.status == 200 and (await applied.json())["count"] == 1
                expected = "Каналы больше не доступны в проекте"
                if changed != "detached":
                    restored = await client.post(f"/api/miniapp/networks/{changed_project}/nodes", headers=headers,
                                                 json={"type": "channel", "object_id": str(CHANNEL)})
                    assert restored.status == 200, await restored.text()
                    expected = "Канал был отвязан после подтверждения"
            elif changed == "manual_reattached":
                node_id = await pool.fetchval(
                    "SELECT id FROM network_nodes WHERE instance_id=$1 AND ref_id=$2", project, CHANNEL)
                project_path = f"/api/miniapp/networks/{project}/nodes"
                assert (await client.delete(f"/api/miniapp/networks/nodes/{node_id}", headers=headers)).status == 200
                restored = await client.post(project_path, headers=headers,
                                             json={"type":"channel", "object_id": str(CHANNEL)})
                assert restored.status == 200, await restored.text()
                expected = "Канал был отвязан после подтверждения"
            elif changed == "revoked":
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "")
                expected = "Доступ к публикациям проекта отозван"
            elif changed == "invalid_targets":
                await pool.execute(
                    """UPDATE operation_queue SET params=params || '{"channel_ids":[]}'::jsonb WHERE id=$1""", op_id)
                expected = "Некорректный список каналов проекта"
            if changed in ("reattached", "manual_reattached"):
                from services import operation_bus as retry_bus

                with pytest.raises(retry_bus.ProjectPublishReviewRequired):
                    await retry_bus.submit(pool, OWNER, "mass_publish", {**params, "cloned_from_op": op_id})
            if productive:
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

            # Сдвигаем только срок в синтетической строке, затем запускаем два тика поллера.
            await pool.execute(
                "UPDATE operation_queue SET scheduled_for=now()-interval '1 second' WHERE id=$1", op_id)
            dispatched = asyncio.Event()
            run_guarded = op_worker._run_op_task_guarded

            async def wait_for_pollers(*args):
                await dispatched.wait()
                await run_guarded(*args)

            with monkeypatch.context() as pollers:
                pollers.setattr(op_worker, "_run_op_task_guarded", wait_for_pollers)
                try:
                    await asyncio.gather(*(op_worker._process_pending(pool, None) for _ in range(2)))
                    tasks = list(op_worker._active_op_tasks)
                    assert [task.get_name() for task in tasks] == [f"op:{op_id}"]
                finally:
                    dispatched.set()
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            state = await pool.fetchrow("SELECT status, result FROM operation_queue WHERE id=$1", op_id)
            assert state["status"] == ("done" if productive else "failed")
            result = json.loads(state["result"])
            if productive:
                send.assert_awaited_once()
                assert send.await_args.args[1] == CHANNEL
                assert result["ok"] == 1
                editor.assert_awaited_once()
                assert await pool.fetchval(
                    "SELECT target FROM operation_log WHERE op_id=$1 AND status='ok'", op_id) == str(CHANNEL)
                following = await pool.fetchrow(
                    """SELECT id, status, params, scheduled_for > now() AS future FROM operation_queue
                        WHERE owner_id=$1 AND id<>$2 AND params->>'project_id'=$3""",
                    OWNER, op_id, str(project),
                )
                assert following["status"] == "pending" and following["future"]
                following_params = json.loads(following["params"])
                assert following_params["channel_ids"] == [CHANNEL]
                assert following_params["_project_scope_since"] == (await pool.fetchval(
                    "SELECT created_at FROM operation_queue WHERE id=$1", op_id)).isoformat()
                stopped.assert_not_awaited()
            else:
                assert expected in result["summary"]
                editor.assert_not_awaited()
                send.assert_not_awaited()
                assert expected in result["recurring_stop_reason"]
                assert "Расписание остановлено" in result["summary"]
                stopped.assert_awaited_once()
                before = await (await client.get(f"/api/miniapp/operation/{op_id}", headers=headers)).json()
                assert before["project_review_required"] is True and before["quick_actions"] == []
                if changed == "revoked":
                    assert "review_project_id" not in before
                    assert "доступ" in before["project_review_message"].lower()
                else:
                    assert before["review_project_id"] == project
                outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
                assert (await client.get(f"/api/miniapp/operation/{op_id}", headers=outsider)).status == 404
                history = await (await client.get(
                    f"/api/miniapp/operations?project_id={project}", headers=headers)).json()
                assert next(row for row in history["operations"] if row["id"] == op_id)["project_review_required"]
                # Даже когда цель снова доступна, восстановитель не продлевает
                # цепочку, которую воркер остановил и сохранил в результате.
                if changed == "detached":
                    await pool.execute(
                        "INSERT INTO network_nodes(instance_id,node_type,ref_id) VALUES($1,'channel',$2)",
                        project, CHANNEL)
                elif changed == "revoked":
                    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
                    await pool.execute(
                        """UPDATE operation_queue SET result=result-'project_publish_scope_refused'
                            WHERE id=$1""", op_id)
                await pool.execute(
                    "UPDATE operation_queue SET finished_at=now()-interval '10 minutes' WHERE id=$1", op_id)
                await op_worker._watchdog_recurring_chain(pool, None)
                from services import operation_bus

                operation = f"/api/miniapp/operation/{op_id}"
                responses = (
                    await client.post(operation + "/retry", headers=headers),
                    await client.get(operation + "/editable", headers=headers),
                    await client.post(operation + "/clone", headers=headers, json={}),
                    await client.post("/api/miniapp/operations/retry_failed", headers=headers),
                )
                for response in responses:
                    assert response.status == 409, await response.text()
                    assert operation_bus.PROJECT_PUBLISH_REVIEW_MESSAGE in await response.text()
                native = await operation_bus.resubmit_one(pool, OWNER, op_id)
                assert native["code"] == 409 and native["reason"] == operation_bus.PROJECT_PUBLISH_REVIEW_MESSAGE
                assert await pool.fetchval(
                    "SELECT count(*) FROM operation_queue WHERE owner_id=$1 AND params->>'project_id'=$2",
                    OWNER, str(project),
                ) == 1
                if changed in ("detached", "reattached", "manual_reattached"):
                    fresh_review = await client.get(
                        f"{preview_path}?project_id={project}&project_channel_id={CHANNEL}", headers=headers)
                    assert fresh_review.status == 200 and (await fresh_review.json())["total"] == 1
                    fresh = await client.post(endpoint, headers=headers, json=payload)
                    assert fresh.status == 200 and (await fresh.json())["op_id"] != op_id
            card = await client.get(f"/api/miniapp/operation/{op_id}", headers=headers)
            assert card.status == 200
            assert expected in await card.text()
            card_body = await card.json()
            if productive:
                assert not card_body.get("project_review_required")
            else:
                assert card_body["review_project_id"] == project
            if changed == "invalid_targets":
                await pool.execute("DELETE FROM network_instances WHERE id=$1 AND owner_id=$2", project, OWNER)
                deleted_project = await (await client.get(
                    f"/api/miniapp/operation/{op_id}", headers=headers)).json()
                assert deleted_project["project_review_required"] is True
                assert "review_project_id" not in deleted_project
                assert deleted_project["project_review_message"] == "Исходный проект недоступен."
            assert await pool.fetchval(
                "SELECT 1 FROM network_nodes WHERE instance_id=$1 AND ref_id=$2", other, CHANNEL)

            if changed == "neighbor_reattached":
                # Следующий круг сохраняет время первоначального подтверждения,
                # даже если прежняя операция уже удалена из истории владельцем.
                original_path = f"/api/miniapp/operations/{check_id}/cleanup"
                detach = {"action": "detach", "project_id": project, "targets": [CHANNEL_TARGET]}
                plan = await client.post(original_path, headers=headers, json={**detach, "preview": True})
                assert plan.status == 200
                applied = await client.post(original_path, headers=headers, json={
                    **detach, "token": (await plan.json())["token"],
                })
                assert applied.status == 200
                assert (await client.post(f"/api/miniapp/networks/{project}/nodes", headers=headers,
                                          json={"type":"channel", "object_id":str(CHANNEL)})).status == 200
                await pool.execute("DELETE FROM operation_queue WHERE id=$1 AND owner_id=$2", op_id, OWNER)
                await pool.execute("UPDATE operation_queue SET created_at=clock_timestamp() WHERE id=$1",
                                   following["id"])
                refused = await op_worker._exec_mass_publish(
                    pool, None, following["id"], OWNER, following_params)
                assert refused["status"] == "failed" and refused["project_publish_scope_refused"]
                assert "Канал был отвязан после подтверждения" in refused["summary"]
                send.assert_awaited_once()

    async def with_synthetic_account(pool, check_id):
        if not productive:
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


def test_project_publication_retry_cannot_add_unconfirmed_targets(monkeypatch):
    from services import operation_bus

    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, _check_id):
        project = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Проект') RETURNING id", OWNER)
        await pool.execute(
            """INSERT INTO managed_channels(owner_id,acc_id,channel_id,title,type)
               VALUES($1,111,$2,'Другой канал','channel'),($1,111,$3,'Поздний канал','channel')""",
            OWNER, CHANNEL + 1, CHANNEL + 2)
        await pool.execute(
            """INSERT INTO network_nodes(instance_id,node_type,ref_id)
               VALUES($1,'channel',$2),($1,'channel',$3),($1,'channel',$4)""",
            project, CHANNEL, CHANNEL + 1, CHANNEL + 2)
        params = {"project_id": project, "channel_ids": [CHANNEL, CHANNEL + 1], "text": "Тест"}
        source = await operation_bus.submit(pool, OWNER, "mass_publish", params, total_items=2)
        await pool.execute("UPDATE operation_queue SET status='failed' WHERE id=$1", source)
        original = operation_bus.params_for_retry(source, params)
        for invalid in (
            {**original, "channel_ids": [CHANNEL + 2]},
            {**original, "project_id": project + 1},
            {**original, "retry_of_op": True},
            {**original, "channel_ids": []},
        ):
            with pytest.raises(operation_bus.ProjectPublishReviewRequired):
                await operation_bus.submit(pool, OWNER, "mass_publish", invalid)
        with pytest.raises(operation_bus.ProjectPublishReviewRequired):
            await operation_bus.submit(pool, OWNER + 1, "mass_publish", original)
        assert await pool.fetchval(
            """SELECT count(*) FROM operation_queue WHERE owner_id=$1 AND params->>'retry_of_op'=$2""",
            OWNER, str(source),
        ) == 0
        retry = await operation_bus.submit(
            pool, OWNER, "mass_publish", {**original, "channel_ids": [str(CHANNEL + 1)]}, total_items=1)
        saved = json.loads(await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", retry))
        assert saved["project_id"] == project and saved["channel_ids"] == [str(CHANNEL + 1)]
        await pool.execute(
            "DELETE FROM network_nodes WHERE instance_id=$1 AND ref_id=$2", project, CHANNEL + 1)
        with pytest.raises(operation_bus.ProjectPublishReviewRequired):
            await operation_bus.submit(
                pool, OWNER, "mass_publish", {**original, "channel_ids": [str(CHANNEL + 1)]}, total_items=1)
        monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "")
        with pytest.raises(operation_bus.ProjectPublishReviewRequired):
            await operation_bus.submit(pool, OWNER, "mass_publish", {**original, "channel_ids": [CHANNEL]})

    asyncio.run(_run(scenario))
