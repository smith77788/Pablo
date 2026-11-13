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
            assert payload["history"] == []
            assert not any("revisions" in item or "bindings" in item for item in payload["findings"])
            assert (await client.post(path, json=action, headers=headers)).status == 400
            for targets in ([], [{"kind": "bot", "id": True}], [BOT_TARGET, BOT_TARGET]):
                invalid = {"action": "disable", "targets": targets, "preview": True}
                assert (await client.post(path, json=invalid, headers=headers)).status == 400
            preview = await client.post(path, json={**action, "preview": True}, headers=headers)
            assert preview.status == 200
            preview_body = await preview.json()
            assert "revisions" not in preview_body["items"][0]
            token = preview_body["token"]
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
            assert "revisions" not in outcome
            reread = await (await client.get(path, headers=headers)).json()
            assert [(entry["action"], entry["target"]) for entry in reread["history"]] == [
                ("disable", f"bot:{BOT}")]
            assert reread["history"][0]["occurred_at"]
            assert reread["history_has_more"] is False
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
            assert (await client.get(path, headers=wrong)).status == 404
            assert not await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            assert (await client.post(path, json=confirmation, headers=headers)).status == 409
            await pool.execute("UPDATE managed_bots SET is_active=TRUE WHERE bot_id=$1", BOT)
            assert (await client.post(path, json=confirmation, headers=headers)).status == 409
            assert await pool.fetchval(
                "SELECT count(*) FROM operation_audit WHERE owner_id=$1 AND operation_id=$2",
                OWNER, op_id) == 1
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


def test_removed_channel_can_be_restored_by_exact_id_through_api(monkeypatch):
    from unittest.mock import AsyncMock

    from bot.utils import subscription
    from database import db
    from services import mini_app_channels

    secret = "123456:synthetic-channel-restore-only"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setattr(subscription, "get_channel_limit", AsyncMock(return_value=10))
    monkeypatch.setattr(subscription, "get_effective_channel_count", AsyncMock(return_value=0))

    async def scenario(pool, op_id):
        assert await db.remove_managed_channel(pool, OWNER, CHANNEL)
        app = web.Application()
        mini_app_channels.setup_routes(app, pool)
        owner = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        other = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        async with TestClient(TestServer(app)) as client:
            removed = await client.get("/api/miniapp/channels/removed", headers=owner)
            item = (await removed.json())["channels"][0]
            assert item["channel_id"] == CHANNEL and item["channel_id_text"] == str(CHANNEL)
            assert "catalog_metadata" not in item and "access_hash" not in item
            path = f"/api/miniapp/channel/{item['channel_id_text']}/restore"
            assert (await client.post(path, headers=other)).status == 404
            response = await client.post(path, headers=owner)
            assert response.status == 200, await response.text()
            assert (await client.post(path, headers=owner)).status == 404

    asyncio.run(_run(scenario))
