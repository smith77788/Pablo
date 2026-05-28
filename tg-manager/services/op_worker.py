"""Фоновый воркер для выполнения очереди операций."""
import asyncio
import json
import logging
import aiohttp
import asyncpg
from aiogram import Bot
from database import db

log = logging.getLogger(__name__)
_POLL_INTERVAL = 15  # секунд между проверками очереди


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Запускается как asyncio.create_task(op_worker.run(pool, bot)) в main.py."""
    log.info("Operation worker started")
    while True:
        try:
            await _process_pending(pool, bot)
        except Exception as e:
            log.exception("op_worker error: %s", e)
        await asyncio.sleep(_POLL_INTERVAL)


async def _process_pending(pool: asyncpg.Pool, bot: Bot) -> None:
    # Взять одну pending операцию (LIMIT 1 чтобы не перегружать)
    row = await pool.fetchrow(
        """SELECT id, owner_id, op_type, params
           FROM operation_queue
           WHERE status = 'pending'
             AND (scheduled_for IS NULL OR scheduled_for <= now())
           ORDER BY created_at ASC
           LIMIT 1
           FOR UPDATE SKIP LOCKED"""
    )
    if not row:
        return

    op_id = row["id"]
    owner_id = row["owner_id"]
    op_type = row["op_type"]
    params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"] or "{}")

    # Пометить как running
    await pool.execute(
        "UPDATE operation_queue SET status='running', started_at=now() WHERE id=$1",
        op_id,
    )

    try:
        # Уведомить пользователя о старте (всегда — это не op_complete, а старт)
        try:
            await bot.send_message(owner_id, f"⚙️ <b>Операция #{op_id}</b> запущена: <code>{op_type}</code>", parse_mode="HTML")
        except Exception:
            pass

        if op_type == "mass_publish":
            result = await _exec_mass_publish(pool, bot, op_id, owner_id, params)
        elif op_type == "bulk_bot_edit":
            result = await _exec_bulk_bot_edit(pool, bot, op_id, owner_id, params)
        else:
            result = {"status": "skipped", "reason": f"unknown op_type: {op_type}"}

        await pool.execute(
            "UPDATE operation_queue SET status='done', finished_at=now(), result=$1::jsonb WHERE id=$2",
            json.dumps(result), op_id,
        )
        summary = result.get("summary", "")
        await db.notify_if_enabled(
            pool, bot, owner_id, "op_complete",
            f"✅ <b>Операция #{op_id}</b> завершена\n{summary}",
        )

    except Exception as e:
        log.exception("op_worker: op %d failed: %s", op_id, e)
        await pool.execute(
            "UPDATE operation_queue SET status='failed', finished_at=now(), error_msg=$1 WHERE id=$2",
            str(e)[:500], op_id,
        )
        await db.notify_if_enabled(
            pool, bot, owner_id, "op_complete",
            f"❌ <b>Операция #{op_id}</b> завершилась с ошибкой:\n<code>{str(e)[:200]}</code>",
        )


async def _exec_mass_publish(pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict) -> dict:
    """Выполнить массовую публикацию."""
    from services import account_manager

    text = params.get("text", "")
    delay = params.get("delay_seconds", 30)
    account_ids = params.get("account_ids") or []

    # Получить аккаунты
    if account_ids:
        accounts = await pool.fetch(
            "SELECT id, session_str, phone, device_model, system_version, app_version "
            "FROM tg_accounts WHERE id = ANY($1) AND is_active=true",
            [int(i) for i in account_ids],
        )
    else:
        accounts = await pool.fetch(
            "SELECT id, session_str, phone, device_model, system_version, app_version "
            "FROM tg_accounts WHERE owner_id=$1 AND is_active=true",
            owner_id,
        )

    total_sent = 0
    total_failed = 0

    for acc in accounts:
        acc_dict = dict(acc)
        try:
            dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc_dict)
            channels = [d for d in (dialogs or []) if d.get("type") in ("channel", "megagroup", "gigagroup")]

            for ch in channels:
                try:
                    await account_manager.post_to_channel(
                        acc["session_str"], ch["id"], text, _acc=acc_dict
                    )
                    total_sent += 1
                    # Log step
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok','sent')",
                        op_id, total_sent, str(ch["id"]),
                    )
                    await pool.execute(
                        "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                    )
                except Exception as e:
                    total_failed += 1
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'error',$4)",
                        op_id, total_sent + total_failed, str(ch["id"]), str(e)[:200],
                    )
                await asyncio.sleep(delay)

        except Exception as e:
            log.warning("op_worker mass_publish: account %s error: %s", acc["phone"], e)

    return {
        "status": "done",
        "sent": total_sent,
        "failed": total_failed,
        "summary": f"Отправлено: {total_sent}, ошибок: {total_failed}",
    }


async def _exec_bulk_bot_edit(pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict) -> dict:
    """Выполнить массовое редактирование ботов через Bot API."""
    field = params.get("field", "")
    value = params.get("value", "")

    bots_rows = await pool.fetch(
        "SELECT id, token FROM bots WHERE owner_id=$1 AND is_active=true", owner_id
    )

    ok_count = 0
    fail_count = 0

    field_to_method = {
        "name": "setMyName",
        "desc": "setMyDescription",
        "short_desc": "setMyShortDescription",
    }
    method = field_to_method.get(field)
    if not method:
        return {"status": "skipped", "reason": f"Unknown field: {field}"}

    async with aiohttp.ClientSession() as sess:
        for b in bots_rows:
            try:
                payload = {"name" if field == "name" else "description" if field == "desc" else "short_description": value}
                resp = await sess.post(
                    f"https://api.telegram.org/bot{b['token']}/{method}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                data = await resp.json()
                if data.get("ok"):
                    ok_count += 1
                    await pool.execute(
                        "INSERT INTO operation_log(op_id, step_num, target, status) VALUES($1,$2,$3,'ok')",
                        op_id, ok_count + fail_count, str(b["id"]),
                    )
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
            await pool.execute(
                "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
            )
            await asyncio.sleep(1)

    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "summary": f"Обновлено: {ok_count} ботов, ошибок: {fail_count}",
    }
