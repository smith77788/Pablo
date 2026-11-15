"""Границы отключения бота через отчёт и архивирования через Mini App."""
import asyncio
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from services import bot_api, mini_app_bots, mini_app_funnels, mini_app_operations, op_worker
from services.mini_app_auth import make_token
from tests.test_restriction_cleanup_postgres import BOT, BOT_TARGET, OWNER, _run, pytestmark

__all__ = ["pytestmark"]


def test_report_bot_reimport_and_atomic_resume_history(monkeypatch):
    from bot.handlers.edit import msg_update_token
    from database import db

    secret = "123456:synthetic-bot-resume"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", f"{OWNER},{OWNER + 1}")
    replacement = f"{BOT}:" + "a" * 35
    telegram_response = AsyncMock()
    telegram_response.__aenter__.return_value = telegram_response
    telegram_response.json.return_value = {
        "ok": True, "result": {"id": BOT, "first_name": "Переимпортированный бот"},
    }

    async def scenario(pool, op_id):
        app = web.Application()
        mini_app_bots.setup_routes(app, pool)
        mini_app_operations.setup_routes(app, pool)
        path = f"/api/miniapp/bot/{BOT}/toggle"
        cleanup = f"/api/miniapp/operations/{op_id}/cleanup"
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        plan = {"action": "disable", "targets": [BOT_TARGET]}
        async with TestClient(TestServer(app)) as client:
            preview = await (await client.post(cleanup, json={**plan, "preview": True}, headers=headers)).json()
            signed = {**plan, "token": preview["token"]}
            assert (await client.post(cleanup, json=signed, headers=headers)).status == 200
            imported = Mock(return_value=telegram_response)
            with monkeypatch.context() as telegram:
                telegram.setattr(ClientSession, "get", imported)
                response = await client.post("/api/miniapp/bot/add", json={"token": replacement}, headers=headers)
                assert response.status == 200, await response.text()
                assert (await response.json())["token_replaced"]
            imported.assert_called_once()
            message = Mock(text=replacement, from_user=Mock(id=OWNER), answer=AsyncMock())
            state = Mock(get_data=AsyncMock(return_value={"old_bot_id": BOT}), clear=AsyncMock())
            identity = AsyncMock(side_effect=[
                {"id": BOT + 1}, {"id": BOT, "first_name": "<Имя бота>"},
            ])
            with monkeypatch.context() as telegram:
                telegram.setattr(bot_api, "get_me", identity)
                await msg_update_token(message, state, pool, None)
                state.clear.assert_not_awaited()
                assert "Не удалось обновить токен" in message.answer.await_args.args[0]
                await msg_update_token(message, state, pool, None)
                state.clear.assert_awaited_once()
                assert "&lt;Имя бота&gt;" in message.answer.await_args.args[0]
            assert await pool.fetchval("SELECT cleanup_paused FROM managed_bots WHERE bot_id=$1", BOT)
            report = await (await client.get(cleanup, headers=headers)).json()
            bot = report["findings"][0]
            assert (bot["id"], bot["status"], bot["active"], bot["can_resume"]) == (
                str(BOT), "restricted", False, True)
            assert replacement not in str(report)
            card = await client.get(f"/api/miniapp/bot/{BOT}", headers=headers)
            assert card.status == 200
            assert "token" not in (await card.json())["bot"]
            assert [entry["action"] for entry in report["history"]] == ["disable"]
            assert (await client.put(path, json={"active": True}, headers=outsider)).status == 404
            assert (await client.put(path, json={"active": "true"}, headers=headers)).status == 400
            with monkeypatch.context() as failed:
                failed.setattr(db, "record_manual_action", AsyncMock(side_effect=RuntimeError("Сбой аудита")))
                assert (await client.put(path, json={"active": True}, headers=headers)).status == 500
            assert not await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            results = await asyncio.gather(*(
                client.put(path, json={"active": True}, headers=headers) for _ in range(2)
            ))
            for result in results:
                assert result.status == 200
                assert (await result.json())["is_active"]
            reread = await (await client.get(cleanup, headers=headers)).json()
            assert reread["findings"][0]["can_disable"] and "can_resume" not in reread["findings"][0]
            assert [(entry["action"], entry["target"]) for entry in reread["history"]] == [
                ("restore", f"bot:{BOT}"), ("disable", f"bot:{BOT}")]
            replay = await client.post(cleanup, json=signed, headers=headers)
            assert replay.status == 200 and (await replay.json())["replayed"]
            assert await pool.fetchval("SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT)
            for expected in (False, True):
                legacy = await client.put(path, headers=headers)
                assert legacy.status == 200 and (await legacy.json())["is_active"] is expected
            after_legacy = await (await client.get(cleanup, headers=headers)).json()
            assert after_legacy["history"] == reread["history"]

    asyncio.run(_run(scenario))


def test_report_pause_archive_and_restore_across_direct_routes(monkeypatch):
    secret = "123456:synthetic-archive-access"
    monkeypatch.setenv("BOT_TOKEN", secret)
    monkeypatch.setenv("INFRAGRAM_PROJECT_PILOT_IDS", str(OWNER))
    commands = AsyncMock(return_value=[])
    delete_webhook = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(bot_api, "get_my_commands", commands)
    monkeypatch.setattr(bot_api, "delete_webhook", delete_webhook)

    async def scenario(pool, op_id):
        app = web.Application()
        mini_app_bots.setup_routes(app, pool)
        mini_app_funnels.setup_routes(app, pool)
        mini_app_operations.setup_routes(app, pool)
        path = f"/api/miniapp/bot/{BOT}"
        cleanup = f"/api/miniapp/operations/{op_id}/cleanup"
        headers = {"Authorization": f"Bearer {make_token(OWNER, secret)}"}
        outsider = {"Authorization": f"Bearer {make_token(OWNER + 1, secret)}"}
        async with TestClient(TestServer(app)) as client:
            async def call(method, suffix, body=None, auth=headers):
                return await client.request(method, path + suffix, headers=auth,
                                            **({"json": body} if body is not None else {}))

            assert (await call("GET", "/audience", auth=outsider)).status == 404
            assert (await call("GET", "/commands")).status == 200
            commands.assert_awaited_once()
            new_items = [
                ("/deeplink", {"name": "Ссылка", "start_param": "synthetic"}),
                ("/funnel", {"name": "Воронка", "first_message": "Привет"}),
                ("/schedule", {"text": "Тест", "execute_at": "2030-01-01T12:00:00+02:00"}),
                ("/auto_reply", {"keyword": "тест", "response_text": "ответ"}),
            ]
            for suffix, body in new_items:
                response = await call("POST", suffix, body)
                assert response.status == 200, (suffix, await response.text())
            funnel_id = await pool.fetchval("SELECT id FROM funnels WHERE bot_id=$1", BOT)
            funnel = f"/api/miniapp/funnel/{funnel_id}"
            added = await client.post(funnel + "/step", headers=headers, json={"message_text": "Шаг 2"})
            assert added.status == 200, await added.text()
            plan = {"action": "disable", "targets": [BOT_TARGET]}
            preview = await client.post(cleanup, json={**plan, "preview": True}, headers=headers)
            assert preview.status == 200
            result = await client.post(cleanup, json={**plan, "token": (await preview.json())["token"]},
                                       headers=headers)
            assert result.status == 200
            assert (await call("GET", "/audience")).status == 200
            assert (await call("GET", "/commands")).status == 404
            assert (await call("DELETE", "/webhook")).status == 404

            assert (await call("DELETE", "")).status == 200
            for suffix in ("/funnels", "/audience", "/schedules", "/deeplinks", "/engagement",
                           "/stats", "/note", "/commands", "/relay/sessions", "/webhook"):
                assert (await call("GET", suffix)).status == 404, suffix
            for suffix, body in new_items + [("/profile", {"name": "Недоступно"})]:
                assert (await call("POST", suffix, body)).status == 404, suffix
            for suffix, body in (("/note", {"note": "Недоступно"}), ("/commands", {"commands": []}),
                                 ("/extra_username", {}), ("/toggle", {"active": True})):
                assert (await call("PUT", suffix, body)).status == 404, suffix
            assert (await client.get(funnel + "/steps", headers=headers)).status == 404
            assert (await client.put(funnel + "/toggle", headers=headers)).status == 404
            assert (await client.post(funnel + "/step", headers=headers, json={"message_text": "Шаг 3"})).status == 404
            assert (await call("DELETE", "/avatar")).status == 404
            assert (await call("DELETE", "/webhook")).status == 404
            commands.assert_awaited_once()
            delete_webhook.assert_not_awaited()
            assert await pool.fetchval("SELECT count(*) FROM funnels WHERE bot_id=$1", BOT) == 1
            assert await pool.fetchval("SELECT count(*) FROM scheduled_broadcasts WHERE bot_id=$1", BOT) == 1

            from database import db
            assert await db.replace_bot_token(pool, OWNER, BOT, "12345:synthetic-recovered")
            assert (await call("GET", "/funnels")).status == 200
            schedules = await call("GET", "/schedules")
            assert schedules.status == 200
            assert (await schedules.json())["schedules"][0]["status"] == "cancelled"

    asyncio.run(_run(scenario))


def test_queued_profile_rollback_skips_bot_paused_after_submission(monkeypatch):
    from database import db

    call = AsyncMock(side_effect=AssertionError("Telegram must not be called"))
    monkeypatch.setattr(bot_api, "_call", call)
    monkeypatch.setattr(op_worker, "_is_cancelled", AsyncMock(return_value=False))

    async def scenario(pool, op_id):
        await pool.execute(
            """INSERT INTO bulk_edit_snapshots(owner_id,op_id,entity_type,target_id,before)
               VALUES($1,$2,'bot',$3,'{"name":"Старое имя"}'::jsonb)""", OWNER, op_id, BOT)
        await db.set_managed_bot_active(pool, BOT, OWNER, False, cleanup_pause=True)
        try:
            result = await op_worker._exec_bulk_bot_profile_rollback(
                pool, None, op_id, OWNER, {"source_op_id": op_id})
            assert result["ok"] == 0 and result["failed"] == 1
            call.assert_not_awaited()
            assert await pool.fetchval(
                "SELECT reverted_at FROM bulk_edit_snapshots WHERE owner_id=$1 AND op_id=$2",
                OWNER, op_id) is None
            assert "недоступен" in await pool.fetchval(
                "SELECT message FROM operation_log WHERE op_id=$1 AND status='error'", op_id)
        finally:
            await pool.execute("DELETE FROM bulk_edit_snapshots WHERE owner_id=$1 AND op_id=$2", OWNER, op_id)

    asyncio.run(_run(scenario))


def test_archive_cancels_schedule_committed_while_waiting_for_bot_lock():
    from database import db

    async def scenario(pool, op_id):
        scheduled_id = await pool.fetchval(
            """INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by, status)
               VALUES($1,'Готовая рассылка',now(),$2,'done') RETURNING id""", BOT, OWNER)
        broadcast_id = await db.create_broadcast(pool, BOT, "Готовая рассылка", 1, OWNER,
                                                 scheduled_id=scheduled_id)
        manual_id = await db.create_broadcast(pool, BOT, "Ручная рассылка", 1, OWNER)
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchval("SELECT bot_id FROM managed_bots WHERE bot_id=$1 FOR SHARE", BOT)
            archive = asyncio.create_task(db.delete_bots(pool, OWNER, [BOT]))
            try:
                for _ in range(100):
                    await conn.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await conn.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                           WHERE wait_event_type='Lock' AND query LIKE 'UPDATE managed_bots SET is_active=FALSE%')""")
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                assert waiting
                schedule = await conn.fetchval(
                    """INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by)
                       VALUES($1,'Тест',now(),$2) RETURNING id""", BOT, OWNER)
            except BaseException:
                archive.cancel()
                await asyncio.gather(archive, return_exceptions=True)
                raise
        assert await asyncio.wait_for(archive, timeout=5) == [BOT]
        assert await pool.fetchval("SELECT status FROM scheduled_broadcasts WHERE id=$1",
                                   schedule) == "cancelled"
        assert await pool.fetchval("SELECT status FROM broadcasts WHERE id=$1", broadcast_id) == "cancelled"
        assert await pool.fetchval("SELECT status FROM broadcasts WHERE id=$1", manual_id) == "pending"

    asyncio.run(_run(scenario))
