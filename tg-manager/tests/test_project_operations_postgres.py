"""Project history and export through the registered Mini App handlers."""
import asyncio
import csv
import io
import json
import os

import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from services import mini_app_api, mini_app_operations, op_params_view

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен тестовый PostgreSQL")
def test_project_history_isolated_and_consistent_with_export(monkeypatch):
    async def exercise():
        conn = await asyncpg.connect(DSN)
        try:
            async with conn.transaction():
                await conn.execute("""
                    CREATE TEMP TABLE network_instances (id int PRIMARY KEY, owner_id bigint);
                    CREATE TEMP TABLE operation_queue (
                        id int PRIMARY KEY, owner_id bigint, op_type text, status text,
                        params jsonb, result jsonb, label text, total_items int, done_items int,
                        retry_count int, error_msg text, last_error text,
                        created_at timestamptz DEFAULT now(), started_at timestamptz,
                        finished_at timestamptz, scheduled_for timestamptz
                    );
                    CREATE TEMP TABLE operation_log (op_id int, status text, target text);
                    INSERT INTO network_instances VALUES (10, 42), (20, 42), (30, 99);
                    INSERT INTO operation_queue (id, owner_id, op_type, status, params, label)
                    VALUES
                        (1, 42, 'mass_publish', 'done', '{"project_id":10}', 'Пост проекта'),
                        (2, 42, 'deploy_network', 'failed', '{"instance_id":10}', 'Развёртывание'),
                        (3, 42, 'mass_publish', 'done', '{"project_id":20}', 'Другой проект'),
                        (4, 99, 'mass_publish', 'done', '{"project_id":10}', 'Чужая запись'),
                        (5, 42, 'mass_publish', 'done', '{"instance_id":10}', 'Обычный пост'),
                        (6, 42, 'mass_publish', 'done', '{}', 'Без проекта');
                """)
                actor = 42
                monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "42,99")
                monkeypatch.setattr(mini_app_api, "_get_uid", lambda _: actor)
                app = web.Application()
                mini_app_operations.setup_routes(app, conn)
                routes = {r.resource.canonical: r.handler for r in app.router.routes()
                          if r.method == "GET"}

                async def get(query="", *, export=False):
                    path = "/api/miniapp/operations" + ("/export" if export else "")
                    return await routes[path](make_mocked_request("GET", path + query))

                result = json.loads((await get("?project_id=10&limit=1")).text)
                assert result["total"] == 2 and result["page"]["has_more"]
                assert result["counts"] == {"done": 1, "failed": 1}
                assert {row["op_type"] for row in result["types"]} == {"mass_publish", "deploy_network"}
                page2 = json.loads((await get("?project_id=10&limit=1&offset=1")).text)
                assert {result["operations"][0]["id"], page2["operations"][0]["id"]} == {1, 2}
                filtered = json.loads((await get("?project_id=10&op_type=mass_publish&status=done&q=Пост")).text)
                assert [row["id"] for row in filtered["operations"]] == [1]
                assert filtered["counts"] == {"done": 1}
                all_rows = json.loads((await get()).text)
                assert {row["id"] for row in all_rows["operations"]} == {1, 2, 3, 5, 6}

                result = json.loads((await get("?project_id=10&format=json", export=True)).text)
                assert result["total"] == 2 and {row["id"] for row in result["operations"]} == {1, 2}
                response = await get("?project_id=10", export=True)
                assert response.status == 200
                assert {int(row[0]) for row in list(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))[1:]} == {1, 2}

                for export in (False, True):
                    assert (await get("?project_id=30", export=export)).status == 404
                    assert (await get("?project_id=garbage", export=export)).status == 400
                    actor = 77
                    assert (await get("?project_id=10", export=export)).status == 403
                    actor = 42
        finally:
            await conn.close()

    asyncio.run(exercise())


def test_project_identity_is_visible_in_operation_details():
    assert {"label": "Проект", "value": "#10"} in op_params_view.describe(
        "mass_publish", {"project_id": 10})
    assert {"label": "Проект", "value": "#10"} in op_params_view.describe(
        "deploy_network", {"instance_id": 10})
    assert op_params_view.describe("mass_publish", {"instance_id": 10}) == []


def test_project_scope_reaches_history_and_export_from_ui():
    from tests.miniapp_source import js_function

    assert "OPS_PROJECT_ID" in js_function("_opsQuery")
    assert "OPS_PROJECT_ID" in js_function("exportOpsCSV")
    assert "openOps(MP_PROJECT_ID)" in js_function("loadMpOps")
    assert "OPS_SEL.clear()" in js_function("openOps")
    assert "revision !== OPS_LOAD_REVISION" in js_function("loadMoreOps")
    assert "revision !== OPS_LOAD_REVISION" in js_function("loadOps")
