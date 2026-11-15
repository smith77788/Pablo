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
    CHANNEL, CHANNEL_TARGET, OWNER, _run, pytestmark,
)

__all__ = ["pytestmark"]


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
