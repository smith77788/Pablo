"""Непустой явный срез проверки и безопасная проверка ёмкости аккаунта."""
import asyncio
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import account_manager, mini_app_accounts, mini_app_api, op_worker, operation_bus
from services.mini_app_auth import make_token


def test_explicit_restriction_check_never_falls_back_to_the_fleet(monkeypatch):
    secret = "123456:synthetic-cleanup-test"
    monkeypatch.setenv("BOT_TOKEN", secret)
    submit = AsyncMock(return_value=12)
    monkeypatch.setattr(operation_bus, "submit", submit)

    async def exercise():
        app = web.Application()
        mini_app_api.setup_routes(app, None)
        headers = {"Authorization": f"Bearer {make_token(42, secret)}"}
        async with TestClient(TestServer(app)) as client:
            path = "/api/miniapp/check_owned_restrictions"
            for body in ([], {"channel_ids": []}, {"bot_ids": None}, {"bot_ids": [-9]},
                         {"bot_ids": [True]}, {"channel_ids": [2**63]}):
                assert (await client.post(path, json=body, headers=headers)).status == 400
            submit.assert_not_awaited()
            response = await client.post(
                path, json={"channel_ids": ["9007199254740993"]}, headers=headers)
            assert response.status == 200, await response.text()
            assert submit.await_args.args[3] == {
                "owner_id": 42, "channel_ids": [9007199254740993], "selection_mode": "exact",
            }
            assert submit.await_args.kwargs["total_items"] == 1

    asyncio.run(exercise())


def test_capacity_endpoint_checks_ownership_and_releases_account(monkeypatch):
    secret = "123456:synthetic-cleanup-test"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", "42,43")
    claim = AsyncMock(return_value=True)
    release = AsyncMock()
    probe = AsyncMock(return_value={"status": "limit_reached", "session_str": "synthetic"})
    monkeypatch.setattr(op_worker, "try_claim_account", claim)
    monkeypatch.setattr(op_worker, "release_accounts", release)
    monkeypatch.setattr(account_manager, "probe_public_channel_capacity", probe)

    class Pool:
        async def fetchval(self, query, acc_id, owner_id):
            assert "owner_id=$2" in query
            return (acc_id, owner_id) == (7, 42)

    async def load(pool, acc_id, owner_id):
        assert (acc_id, owner_id) == (7, 42)
        return {"id": acc_id, "session_str": "synthetic"}

    monkeypatch.setattr("database.db.get_account_for_telethon", load)

    async def exercise():
        app = web.Application()
        mini_app_accounts.setup_routes(app, Pool())
        async with TestClient(TestServer(app)) as client:
            path = "/api/miniapp/account/7/public_channel_capacity"
            assert (await client.post(path)).status == 401
            headers = {"Authorization": f"Bearer {make_token(43, secret)}"}
            assert (await client.post(path, headers=headers)).status == 404
            claim.assert_not_awaited()
            headers = {"Authorization": f"Bearer {make_token(42, secret)}"}
            claim.return_value = False
            busy = await client.post(path, headers=headers)
            assert (await busy.json())["reason"] == "busy"
            probe.assert_not_awaited()
            claim.return_value = True
            response = await client.post(path, headers=headers)
            result = await response.json()
            assert result["status"] == "limit_reached" and result["checked_at"]
            assert "synthetic" not in str(result)
            release.assert_awaited_once_with([7])

    asyncio.run(exercise())
