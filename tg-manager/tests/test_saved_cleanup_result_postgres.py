"""Повторное чтение результата очистки без POST и исходной подписи."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from database import db
from services import mini_app_operations
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import (
    BOT, BOT_TARGET, CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark,
)

__all__ = ["pytestmark"]


def test_incomplete_archive_rolls_back_mixed_cleanup(monkeypatch):
    secret = "123456:synthetic-cleanup-rollback"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    archive_batch = db.remove_managed_channels_on_connection

    async def archive_with_missing_target(*args, **kwargs):
        assert await archive_batch(*args, **kwargs) == {CHANNEL}
        return set()

    async def scenario(pool, op_id):
        await pool.execute(
            "INSERT INTO va_channel_admin(owner_id,channel_id,enabled) VALUES($1,$2,TRUE)", OWNER, CHANNEL,
        )
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        action = {"action": "disable", "targets": [BOT_TARGET, CHANNEL_TARGET]}
        async with TestClient(TestServer(app)) as client:
            preview = await client.post(path, headers=headers, json={**action, "preview": True})
            assert preview.status == 200
            signed = {**action, "token": (await preview.json())["token"]}
            with monkeypatch.context() as incomplete:
                incomplete.setattr(db, "remove_managed_channels_on_connection", archive_with_missing_target)
                response = await client.post(path, headers=headers, json=signed)
                assert response.status == 409
            assert await pool.fetchval(
                "SELECT is_active FROM managed_bots WHERE added_by=$1 AND bot_id=$2", OWNER, BOT,
            )
            assert await pool.fetchval(
                "SELECT enabled FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
            )
            assert not await pool.fetchval(
                "SELECT 1 FROM removed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
            )
            assert not await pool.fetchval("SELECT 1 FROM operation_audit WHERE operation_id=$1", op_id)
            report = await (await client.get(path, headers=headers)).json()
            assert not report["receipts"]
            applied = await client.post(path, headers=headers, json=signed)
            assert applied.status == 200, await applied.text()
            assert (await applied.json())["count"] == 2

    asyncio.run(_run(scenario))


def test_saved_result_preserves_applied_and_skipped_targets_across_sessions(monkeypatch):
    secret = "123456:synthetic-saved-cleanup"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, operation_id):
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        outside = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        session = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        path = f"/api/miniapp/operations/{operation_id}/cleanup"
        action = {"action": "disable", "targets": [
            CHANNEL_TARGET, {"kind": "channel", "id": "99"},
        ]}
        second = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id,op_type,status,result)
               VALUES($1,'check_owned_restrictions','done','{}') RETURNING id""", OWNER)
        async with TestClient(TestServer(app)) as first:
            old_report = await (await first.get(path, headers=session)).json()
            assert old_report["receipts"] == []
            preview = await first.post(path, headers=session, json={**action, "preview": True})
            assert preview.status == 200
            confirmation = (await preview.json())["token"]
            applied = await first.post(path, headers=session, json={**action, "token": confirmation})
            assert applied.status == 200
            applied_body = await applied.json()
            assert {row["id"]: (row["status"], row["outcome"]) for row in applied_body["items"]} == {
                str(CHANNEL): ("check_failed", "applied"), "99": ("restricted", "skipped"),
            }
            async with pool.acquire() as busy, busy.transaction():
                await busy.execute("LOCK TABLE operation_queue IN SHARE ROW EXCLUSIVE MODE")
                replay = await asyncio.wait_for(
                    first.post(path, headers=session, json={**action, "token": confirmation}), timeout=2,
                )
                assert replay.status == 200
                assert await replay.json() == {**applied_body, "replayed": True}

        assert await db.restore_removed_channel(pool, OWNER, CHANNEL)
        second_app = web.Application()
        mini_app_operations.setup_routes(second_app, pool)
        async with TestClient(TestServer(second_app)) as browser:
            report = await (await browser.get(path, headers=session)).json()
            summary, = report["receipts"]
            assert summary["saved_at"] and (summary["changed"], summary["total"]) == (1, 2)
            assert confirmation not in json.dumps(report)
            assert "items" not in summary
            saved_path = f"{path}?receipt={summary['id']}"
            for invalid in ("", "a" * 63, "Z" * 64, f"{summary['id']}&receipt={summary['id']}"):
                assert (await browser.get(f"{path}?receipt={invalid}", headers=session)).status == 400
            assert (await browser.get(saved_path)).status == 401
            assert (await browser.get(saved_path, headers=outside)).status == 404
            assert (await browser.get(saved_path.replace(f"/{operation_id}/", f"/{second}/"),
                                      headers=session)).status == 404
            assert (await browser.post(saved_path, headers=session,
                                       json={**action, "token": confirmation})).status == 400
            record = await (await browser.get(saved_path, headers=session)).json()
            assert record == {**applied_body, "replayed": True}
            assert "synthetic-restore-session" not in str(record)
            assert await pool.fetchval(
                "SELECT id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL)
            assert await pool.fetchval(
                """SELECT count(*) FROM operation_audit WHERE operation_id=$1 AND owner_id=$2
                     AND action='restriction_cleanup_disable'""", operation_id, OWNER) == 1

            # Совместимость записей до появления даты и ограничение списка по размеру.
            archived = {key: val for key, val in applied_body.items() if key != "saved_at"}
            await pool.executemany(
                """INSERT INTO restriction_cleanup_receipts
                       (operation_id,owner_id,confirmation_id,request_scope,response)
                   VALUES($1,$2,$3,'{}',$4::jsonb)""",
                [(operation_id, OWNER, f"{number:064x}", json.dumps(archived)) for number in range(27)])
            bounded = await (await browser.get(path, headers=session)).json()
            assert bounded["receipts_has_more"] and len(bounded["receipts"]) == 25
            assert bounded["receipts"][0] == summary
            legacy_id = bounded["receipts"][1]["id"]
            assert bounded["receipts"][1]["saved_at"] is None
            old_save = await (await browser.get(f"{path}?receipt={legacy_id}", headers=session)).json()
            assert old_save == {**archived, "replayed": True}
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
            assert (await browser.get(saved_path, headers=session)).status == 403

    asyncio.run(_run(scenario))


def test_expired_confirmation_only_reopens_the_exact_saved_result(monkeypatch):
    from types import SimpleNamespace

    from services import channel_change_approval as approval

    secret = "123456:synthetic-expired-cleanup"
    start = int(approval.time.time())
    clock = [start]
    monkeypatch.setattr(approval, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")

    async def scenario(pool, op_id):
        app = web.Application()
        mini_app_operations.setup_routes(app, pool)
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        path = f"/api/miniapp/operations/{op_id}/cleanup"
        by_channel = {"action": "disable", "targets": [CHANNEL_TARGET]}
        by_bot = {"action": "disable", "targets": [BOT_TARGET]}
        async with TestClient(TestServer(app)) as client:
            first = await client.post(path, headers=headers, json={**by_channel, "preview": True})
            second = await client.post(path, headers=headers, json={**by_bot, "preview": True})
            used = {**by_channel, "token": (await first.json())["token"]}
            unused = {**by_bot, "token": (await second.json())["token"]}
            assert used["token"] and unused["token"] and used["token"] != unused["token"]
            applied = await client.post(path, headers=headers, json=used)
            assert applied.status == 200
            result = await applied.json()
            # После потери ответа пользователь мог уже вернуть ресурс из архива.
            assert await db.restore_removed_channel(pool, OWNER, CHANNEL)
            clock[0] += 601
            replayed = await client.post(path, headers=headers, json=used)
            assert replayed.status == 200, await replayed.text()
            assert await replayed.json() == {**result, "replayed": True}
            for rejected in (
                unused, {**used, "targets": [BOT_TARGET]},
                {**used, "action": "detach", "project_id": 7},
                {**used, "token": used["token"] + "0"},
            ):
                assert (await client.post(path, headers=headers, json=rejected)).status == 409
            assert (await client.post(path, headers=outsider, json=used)).status == 409
            assert (await client.post(path.replace(f"/{op_id}/", f"/{op_id+1}/"),
                                      headers=headers, json=used)).status == 409
            assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1 AND added_by=$2", BOT, OWNER)
            assert await pool.fetchval(
                "SELECT id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2", OWNER, CHANNEL,
            )
            assert await pool.fetchval(
                "SELECT count(*) FROM restriction_cleanup_receipts WHERE owner_id=$1 AND operation_id=$2", OWNER, op_id,
            ) == 1
            monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER + 1))
            assert (await client.post(path, headers=headers, json=used)).status == 403

    asyncio.run(_run(scenario))
