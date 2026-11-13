"""Подписанный HTTP, настоящая очередь PostgreSQL и имитация Telegram."""
import asyncio
import json
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import account_manager, mini_app_networks, mini_app_operations, op_worker, operation_bus
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import OWNER, _run, pytestmark

__all__ = ["pytestmark"]


def test_project_deployment_confirms_exact_topology_and_executor(monkeypatch):
    secret = "123456:synthetic-project-deployment"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    monkeypatch.setattr(operation_bus, "_enforce_min_plan", AsyncMock(return_value=False))
    monkeypatch.setattr(operation_bus, "_enforce_immunity", AsyncMock())
    create = AsyncMock(return_value={"error": "тайм-аут", "requires_reconciliation": True})
    monkeypatch.setattr(account_manager, "create_channel", create)
    monkeypatch.setattr(op_worker, "try_claim_account", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "release_accounts", AsyncMock())

    async def scenario(pool, _):
        accounts = []
        for i, owner in enumerate((OWNER, OWNER, OWNER + 1)):
            accounts.append(await pool.fetchval(
                """INSERT INTO tg_accounts(owner_id,phone,first_name,session_str)
                   VALUES($1,$2,'Исполнитель','synthetic-session') RETURNING id""", owner, f"test-deploy-{i}"))
        executor, other, foreign = accounts
        try:
            project = await pool.fetchval(
                "INSERT INTO network_instances(owner_id,name) VALUES($1,'Проект') RETURNING id", OWNER)
            node = await pool.fetchval(
                """INSERT INTO network_nodes(instance_id,node_type,label)
                   VALUES($1,'channel','Канал') RETURNING id""", project)
            app = web.Application()
            mini_app_networks.setup_routes(app, pool)
            mini_app_operations.setup_routes(app, pool)
            path = f"/api/miniapp/networks/{project}"
            headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
            outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
            async with TestClient(TestServer(app)) as client:
                async def preview(account):
                    response = await client.get(f"{path}/deploy_plan?account_id={account}", headers=headers)
                    assert response.status == 200, await response.text()
                    plan = await response.json()
                    assert "synthetic-session" not in str(plan)
                    assert plan["executor"]["id"] == account
                    return {"account_id": account, "approval": plan["approval"]}

                async def submit(body):
                    return await client.post(path + "/deploy", headers=headers, json=body)

                assert (await client.get(path + "/deploy_plan", headers=outsider)).status == 404
                assert (await client.get(f"{path}/deploy_plan?account_id={foreign}", headers=headers)).status == 404
                assert (await submit({})).status == 400
                approval = await preview(executor)
                assert (await submit({**approval, "account_id": other})).status == 409
                assert (await submit({**approval, "approval": approval["approval"] + "0"})).status == 409
                await pool.execute("UPDATE network_nodes SET label='Новый канал' WHERE id=$1", node)
                assert (await submit(approval)).status == 409
                approval = await preview(executor)
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "")
                assert (await submit(approval)).status == 403
                legacy = await submit({})
                assert legacy.status == 200, await legacy.text()
                legacy_id = (await legacy.json())["op_id"]
                await pool.execute("UPDATE operation_queue SET status='cancelled' WHERE id=$1", legacy_id)
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))

                first, second = await asyncio.gather(submit(approval), submit(approval))
                assert first.status == second.status == 200
                op_id = (await first.json())["op_id"]
                assert op_id == (await second.json())["op_id"]
                assert (await submit(await preview(other))).status == 403
                params = json.loads(await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", op_id))
                assert set(params) == {"instance_id", "account_id", "topology_digest", "approval_digest"}
                operation = f"/api/miniapp/operation/{op_id}"
                assert (await client.get(operation, headers=headers)).status == 200
                assert (await client.get(operation, headers=outsider)).status == 404
                assert (await client.post(operation + "/retry", headers=headers)).status == 409
                assert (await client.post(operation + "/clone", headers=headers, json={})).status == 409

                await pool.execute("UPDATE network_nodes SET label='Изменён' WHERE id=$1", node)
                stale = await op_worker._exec_deploy_network(pool, None, op_id, OWNER, params)
                assert stale["status"] == "failed" and "Топология изменилась" in stale["summary"]
                await pool.execute("UPDATE network_nodes SET label='Новый канал' WHERE id=$1", node)
                await pool.execute("UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", executor)
                unavailable = await op_worker._exec_deploy_network(pool, None, op_id, OWNER, params)
                assert "аккаунт недоступен" in unavailable["summary"]
                await pool.execute("UPDATE tg_accounts SET is_active=TRUE WHERE id=$1", executor)
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "")
                revoked = await op_worker._exec_deploy_network(pool, None, op_id, OWNER, params)
                assert "отозван" in revoked["summary"]
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
                create.assert_not_awaited()

                first_run = await op_worker._exec_deploy_network(pool, None, op_id, OWNER, params)
                assert "Требуется сверка" in first_run["summary"]
                create.assert_awaited_once()
                assert create.await_args.kwargs["_acc"]["id"] == executor
                recovery = await op_worker._exec_deploy_network(pool, None, op_id, OWNER, params)
                assert "Требуется сверка" in recovery["summary"]
                create.assert_awaited_once()
                await pool.execute("UPDATE operation_queue SET status='failed' WHERE id=$1", op_id)
                replay = await submit(approval)
                assert replay.status == 200 and (await replay.json())["op_id"] == op_id
                assert await pool.fetchval(
                    "SELECT count(*) FROM operation_log WHERE op_id=$1 AND target='deploy_start'", op_id) == 1
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE id=ANY($1::int[])", accounts)

    asyncio.run(_run(scenario))
