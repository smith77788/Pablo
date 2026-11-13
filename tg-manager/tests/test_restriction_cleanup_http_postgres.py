"""Подписанная сессия Mini App и точное подтверждение на тестовом PostgreSQL."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import mini_app_operations
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
            assert (await client.post(path, json=action, headers=headers)).status == 400
            for targets in ([], [{"kind": "bot", "id": True}], [BOT_TARGET, BOT_TARGET]):
                invalid = {"action": "disable", "targets": targets, "preview": True}
                assert (await client.post(path, json=invalid, headers=headers)).status == 400
            preview = await client.post(path, json={**action, "preview": True}, headers=headers)
            assert preview.status == 200
            token = (await preview.json())["token"]
            assert secret not in token
            changed = {**action, "targets": [CHANNEL_TARGET], "token": token}
            assert (await client.post(path, json=changed, headers=headers)).status == 409
            confirmation = {**action, "token": token}
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
            assert (await client.post(path, json=confirmation, headers=headers)).status == 403
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
            response = await client.post(path, json=confirmation, headers=headers)
            assert response.status == 200, await response.text()
            outcome = (await response.json())["items"][0]
            assert outcome["id"] == str(BOT) and outcome["outcome"] in ("applied", "disabled")
            assert not await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            assert (await client.post(path, json=confirmation, headers=headers)).status == 409
            await pool.execute("UPDATE managed_bots SET is_active=TRUE WHERE bot_id=$1", BOT)
            assert (await client.post(path, json=confirmation, headers=headers)).status == 409
            assert await pool.fetchval(
                "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2",
                OWNER, op_id) == 1

    asyncio.run(_run(scenario))
