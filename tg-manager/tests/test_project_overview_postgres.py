"""Стартовый обзор проектов через подписанный Mini App API."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import mini_app_networks, mini_app_operations
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import BOT, CHANNEL, OWNER, _run, pytestmark

__all__ = ["pytestmark"]


def test_project_overview_isolated_and_links_to_existing_history(monkeypatch):
    secret = "123456:synthetic-project-overview"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, _):
        older = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'С проблемами') RETURNING id", OWNER)
        newer = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Новый') RETURNING id", OWNER)
        foreign = await pool.fetchval(
            "INSERT INTO network_instances(owner_id,name) VALUES($1,'Чужой') RETURNING id", OWNER + 1)
        try:
            await pool.execute(
                """INSERT INTO network_nodes(instance_id,node_type,ref_id)
                   VALUES($1,'channel',$2),($1,'bot',$3),($1,'group',$2),($1,'channel',NULL)""",
                older, CHANNEL, BOT,
            )
            await pool.execute(
                """INSERT INTO operation_queue(owner_id,op_type,status,params,created_at)
                   VALUES
                   ($1,'deploy_network','partial',
                    jsonb_build_object('instance_id',$3::int,'project_id',$3::int),now()),
                   ($1,'mass_publish','failed',jsonb_build_object('project_id',$3::int),now()-interval '8 days'),
                   ($1,'mass_publish','pending',jsonb_build_object('project_id',$3::int),now()),
                   ($1,'mass_publish','failed',jsonb_build_object('instance_id',$4::int),now()),
                   ($2,'mass_publish','failed',jsonb_build_object('project_id',$3::int),now())""",
                OWNER, OWNER + 1, older, newer,
            )
            app = web.Application()
            mini_app_networks.setup_routes(app, pool)
            mini_app_operations.setup_routes(app, pool)
            base = "/api/miniapp"
            path = base + "/networks/project_overview"
            owner = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
            outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
            async with TestClient(TestServer(app)) as client:
                assert (await client.get(path)).status == 401
                assert (await client.get(path + "?limit=0", headers=owner)).status == 400
                assert (await client.get(path + "?limit=21", headers=owner)).status == 400
                response = await client.get(path + "?limit=1", headers=owner)
                assert response.status == 200, await response.text()
                result = await response.json()
                assert result["enabled"] and result["total"] == 2 and result["issue_days"] == 7
                project, = result["projects"]
                assert project == {"id": older, "name": "С проблемами", "node_count": 4,
                                   "unavailable": 1, "in_flight": 1, "failed": 1,
                                   "issue_id": project["issue_id"]}
                assert project["issue_id"].isdigit()
                latest = await client.get(base + "/operation/" + project["issue_id"], headers=owner)
                assert (await latest.json())["status"] == "partial"
                journal = await client.get(f"{base}/operations?project_id={older}", headers=owner)
                assert (await journal.json())["total"] == 3
                own_list = await (await client.get(path, headers=owner)).json()
                assert [item["id"] for item in own_list["projects"]] == [older, newer]
                assert own_list["projects"][1]["issue_id"] is None
                others = await (await client.get(path, headers=outsider)).json()
                assert [item["id"] for item in others["projects"]] == [foreign]
                assert others["projects"][0]["failed"] == 0
                await pool.execute("DELETE FROM network_instances WHERE id=$1", foreign)
                empty = await (await client.get(path, headers=outsider)).json()
                assert empty == {"enabled": True, "projects": [], "total": 0, "issue_days": 7}
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
                assert await (await client.get(path, headers=owner)).json() == {"enabled": False}
        finally:
            await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1 AND params->>'project_id'=$2",
                               OWNER + 1, str(older))
            await pool.execute("DELETE FROM network_instances WHERE id=$1", foreign)

    asyncio.run(_run(scenario))
