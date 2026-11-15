"""Закрепление поста через штатную очередь после изменения каталога."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from database import db
from services import account_manager, mini_app_channels, mini_app_operations, op_worker
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark

__all__ = ["pytestmark"]


@pytest.mark.parametrize("case", [
    "success", "legacy", "disabled", "restored", "reassigned", "inactive", "uncertain", "journal_lost", "rejected",
])
def test_cleanup_and_pin_through_signed_http(monkeypatch, case):
    secret = "123456:synthetic-cleanup-pin"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    monkeypatch.setattr("bot.utils.subscription.require_plan", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "circuit_breaker_status", AsyncMock(return_value={}))
    monkeypatch.setattr(op_worker, "_circuit_breaker_record", AsyncMock())
    monkeypatch.setattr(op_worker, "try_claim_account", AsyncMock(return_value=True))
    monkeypatch.setattr(op_worker, "release_accounts", AsyncMock())
    monkeypatch.setattr(db, "notify_if_enabled", AsyncMock())
    if case == "journal_lost":
        monkeypatch.setattr(op_worker, "_journal_done", AsyncMock(return_value=False))

    async def scenario(pool, check_id):
        app = web.Application()
        mini_app_channels.setup_routes(app, pool)
        mini_app_operations.setup_routes(app, pool)
        path = f"/api/miniapp/channel/{CHANNEL}"
        cleanup_path = f"/api/miniapp/operations/{check_id}/cleanup"
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        outside = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        action = {"action": "disable", "targets": [CHANNEL_TARGET]}
        calls = []

        async def pin(session, channel_id, *, before_pin, access_hash, username, **kwargs):
            assert session == "synthetic-restore-session"
            assert (channel_id, access_hash, username) == (CHANNEL, 42, "synthetic")
            await before_pin(77)
            saved = await pool.fetchval(
                "SELECT message FROM operation_log WHERE op_id=$1 AND status='started'", op_id)
            assert saved == "Запрос на закрепление поста #77"
            calls.append(channel_id)
            if case == "uncertain":
                raise TimeoutError("synthetic-restore-session")
            if case == "rejected":
                return {"error": "Требуются права администратора", "pin_rejected": True}
            return {"pinned_msg_id": 77}

        monkeypatch.setattr(account_manager, "pin_last_channel_post", pin)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post(path + "/pin", headers=outside)).status == 404
            assert (await client.post("/api/miniapp/channel/9223372036854775808/pin",
                                      headers=headers)).status == 400
            detail = await (await client.get(path, headers=headers)).json()
            assert detail["channel"]["channel_id_text"] == str(CHANNEL)
            row_id = await pool.fetchval("SELECT id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                                         OWNER, CHANNEL)

            async def submit():
                response = await client.post(path + "/pin", headers=headers)
                assert response.status == 200, await response.text()
                return (await response.json())["op_id"]

            op_id, duplicate = await asyncio.gather(submit(), submit())
            assert op_id == duplicate
            params = json.loads(await pool.fetchval("SELECT params FROM operation_queue WHERE id=$1", op_id))
            assert params["channel_ref"] == CHANNEL and params["managed_channel_row_id"] == row_id
            assert "access_hash" not in params
            if case == "legacy":
                params.pop("managed_channel_row_id")
                params.update(channel_ref=str(CHANNEL), account_id="992660", access_hash=99)
                await pool.execute("UPDATE operation_queue SET params=$2::jsonb WHERE id=$1",
                                   op_id, json.dumps(params))

            await pool.execute(
                "UPDATE managed_channels SET access_hash=42, username='synthetic' "
                "WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL)
            review = await client.post(cleanup_path, headers=headers, json={**action, "preview": True})
            assert review.status == 200, await review.text()
            confirmation = {**action, "token": (await review.json())["token"]}
            if case in ("disabled", "restored"):
                applied = await client.post(cleanup_path, headers=headers, json=confirmation)
                assert applied.status == 200 and (await applied.json())["count"] == 1
                if case == "restored":
                    assert await db.restore_removed_channel(pool, OWNER, CHANNEL)
            elif case == "reassigned":
                await pool.execute(
                    "UPDATE managed_channels SET acc_id=992661 WHERE owner_id=$1 AND channel_id=$2",
                    OWNER, CHANNEL)
            elif case == "inactive":
                await pool.execute("UPDATE tg_accounts SET is_active=FALSE WHERE id=992660 AND owner_id=$1", OWNER)
            else:
                await pool.execute("UPDATE operation_queue SET status='running' WHERE id=$1", op_id)
                busy = await client.post(cleanup_path, headers=headers, json={**action, "preview": True})
                plan = await busy.json()
                assert plan["token"] is None and "команды управления" in plan["items"][0]["reason"]
                assert (await client.post(cleanup_path, headers=headers, json=confirmation)).status == 409
                await pool.execute("UPDATE operation_queue SET status='pending' WHERE id=$1", op_id)

            excluded = {row["id"] for row in await pool.fetch(
                "SELECT id FROM operation_queue WHERE status='pending' AND id<>$1", op_id)}
            monkeypatch.setattr(op_worker, "_active_op_ids", excluded)
            monkeypatch.setattr(op_worker, "_active_op_tasks", set())
            monkeypatch.setattr(op_worker, "_MAX_PARALLEL", len(excluded) + 2)
            await op_worker._process_pending(pool, None)
            tasks = list(op_worker._active_op_tasks)
            assert [task.get_name() for task in tasks] == [f"op:{op_id}"]
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            card_path = f"/api/miniapp/operation/{op_id}"
            card = await (await client.get(card_path, headers=headers)).json()
            log = await (await client.get(card_path + "/log", headers=headers)).json()
            assert (await client.get(card_path + "/log", headers=outside)).status == 404
            export = await client.get(card_path + "/log.csv", headers=headers)
            assert export.status == 200
            assert "synthetic-restore-session" not in str((card, log, await export.text()))
            if case in ("uncertain", "journal_lost", "restored"):
                retry_params = {**params, "retry_of_op": op_id}
                retry_id = await pool.fetchval(
                    """INSERT INTO operation_queue(owner_id,op_type,status,params)
                       VALUES($1,'pin_last_post','pending',$2::jsonb) RETURNING id""",
                    OWNER, json.dumps(retry_params))
                replay = await op_worker._exec_pin_last_post(pool, None, retry_id, OWNER, retry_params)
                assert replay["status"] == "failed"
                expected = "Требуется сверка" if case != "restored" else "восстановлен после постановки"
                assert expected in replay["reason"]
            if case in ("success", "legacy", "uncertain", "journal_lost", "rejected"):
                assert calls == [CHANNEL] and all(row["target"] == str(CHANNEL) for row in log["logs"])
                started = await (await client.get(card_path + "/log?status=started", headers=headers)).json()
                assert [row["status"] for row in started["logs"]] == ["started"]
                if case != "rejected":
                    replay = await op_worker._exec_pin_last_post(pool, None, op_id, OWNER, params)
                    assert calls == [CHANNEL]
                    if case in ("uncertain", "journal_lost"):
                        assert [row["status"] for row in log["logs"]] == ["started"]
                        assert card["status"] == "failed" and "Требуется сверка" in replay["reason"]
                    else:
                        assert {row["status"] for row in log["logs"]} == {"started", "ok"}
                        assert card["status"] == replay["status"] == "done"
                        assert "#77" in replay["summary"]
                else:
                    assert card["status"] == "failed"
                    assert {row["status"] for row in log["logs"]} == {"started", "error"}
            else:
                assert card["status"] == "failed" and not calls
                assert "Канал отключён, восстановлен после постановки" in str(card)

    asyncio.run(_run(scenario))


@pytest.mark.parametrize("case", ["wrong_peer", "journal_unavailable", "telegram_timeout", "success"])
def test_pin_records_intent_before_telegram_mutation(monkeypatch, case):
    from types import SimpleNamespace
    from telethon.tl.functions import messages

    events = []

    class Client:
        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def get_messages(self, peer, *, limit):
            assert limit == 1
            events.append("read")
            return [SimpleNamespace(id=77)]

        async def __call__(self, request):
            assert (request.peer.channel_id, request.id) == (CHANNEL, 77)
            events.append("send")
            if case == "telegram_timeout":
                raise TimeoutError

    async def resolve(client, reference, access_hash):
        assert reference == "@synthetic"
        return SimpleNamespace(channel_id=CHANNEL + (case == "wrong_peer"))

    async def mark(message_id):
        assert message_id == 77
        events.append("mark")
        if case == "journal_unavailable":
            raise RuntimeError("synthetic journal outage")

    monkeypatch.setattr(account_manager, "_make_client", lambda *a: Client())
    monkeypatch.setattr(account_manager, "_resolve_channel_peer", resolve)
    monkeypatch.setattr(messages, "UpdatePinnedMessageRequest", SimpleNamespace)
    result = asyncio.run(account_manager.pin_last_channel_post(
        "synthetic", CHANNEL, username="synthetic", before_pin=mark))
    assert events == {
        "wrong_peer": [], "journal_unavailable": ["read", "mark"],
        "telegram_timeout": ["read", "mark", "send"], "success": ["read", "mark", "send"],
    }[case]
    assert result.get("pinned_msg_id") == (77 if case == "success" else None)
