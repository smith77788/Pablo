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


async def _is_cancelled(pool: asyncpg.Pool, op_id: int) -> bool:
    """Check if operation was cancelled by user."""
    row = await pool.fetchrow("SELECT status FROM operation_queue WHERE id=$1", op_id)
    return row and row["status"] == "cancelled"


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
        elif op_type == "bulk_join":
            result = await _exec_bulk_join(pool, bot, op_id, owner_id, params)
        elif op_type == "bulk_leave":
            result = await _exec_bulk_leave(pool, bot, op_id, owner_id, params)
        elif op_type == "global_presence_channel":
            result = await _exec_global_presence_channel(pool, bot, op_id, owner_id, params)
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
            "SELECT a.id, a.session_str, a.phone, a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.id = ANY($1) AND a.is_active=true",
            [int(i) for i in account_ids],
        )
    else:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.phone, a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.is_active=true",
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


async def _exec_bulk_join(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Вступить в список каналов/групп несколькими аккаунтами."""
    from services import account_manager
    import random

    links = params.get("links", [])
    account_ids = params.get("account_ids") or []

    if account_ids:
        accounts = await pool.fetch(
            "SELECT id, session_str, phone, device_model, system_version, app_version "
            "FROM tg_accounts WHERE id = ANY($1) AND owner_id=$2 AND is_active=true",
            [int(i) for i in account_ids], owner_id,
        )
    else:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.phone, a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.is_active=true",
            owner_id,
        )

    ok_count = 0
    fail_count = 0
    step = 0

    from services import session_simulator
    for acc in accounts:
        acc_dict = dict(acc)
        for i, link in enumerate(links):
            # Check for cancellation before each step
            if await _is_cancelled(pool, op_id):
                return {"status": "cancelled", "ok": ok_count, "failed": fail_count,
                        "summary": f"Отменено. Вступлено: {ok_count}, ошибок: {fail_count}"}
            step += 1
            try:
                res = await account_manager.join_channel(acc["session_str"], link, _acc=acc_dict)
                if res.get("error"):
                    raise Exception(res["error"])
                ok_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok','joined')",
                    op_id, step, link,
                )
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
            except Exception as e:
                fail_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id, step, link, str(e)[:200],
                )
            # Human-like anti-flood: 45-120s between joins, extra cooldown every 5
            if i % 5 == 4:
                await asyncio.sleep(random.uniform(180, 360) * session_simulator.chaos_factor())
            else:
                await asyncio.sleep(random.uniform(45, 120) * session_simulator.chaos_factor())

    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "summary": f"Вступлено: {ok_count}, ошибок: {fail_count}",
    }


async def _exec_bulk_leave(
    pool: asyncpg.Pool, bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выйти из списка каналов/групп несколькими аккаунтами."""
    from services import account_manager
    import random

    channels = params.get("channels", [])
    account_ids = params.get("account_ids") or []

    if account_ids:
        accounts = await pool.fetch(
            "SELECT id, session_str, phone, device_model, system_version, app_version "
            "FROM tg_accounts WHERE id = ANY($1) AND owner_id=$2 AND is_active=true",
            [int(i) for i in account_ids], owner_id,
        )
    else:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.phone, a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.is_active=true",
            owner_id,
        )

    ok_count = 0
    fail_count = 0
    step = 0

    from services import session_simulator
    for acc in accounts:
        acc_dict = dict(acc)
        for i, channel in enumerate(channels):
            # Check for cancellation before each step
            if await _is_cancelled(pool, op_id):
                return {"status": "cancelled", "ok": ok_count, "failed": fail_count,
                        "summary": f"Отменено. Вышли: {ok_count}, ошибок: {fail_count}"}
            step += 1
            try:
                await account_manager.leave_channel(acc["session_str"], channel, _acc=acc_dict)
                ok_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok','left')",
                    op_id, step, str(channel),
                )
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
            except Exception as e:
                fail_count += 1
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id, step, str(channel), str(e)[:200],
                )
            # Human-like delay: 15-45s between leaves
            await asyncio.sleep(random.uniform(15, 45) * session_simulator.chaos_factor())

    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "summary": f"Вышли: {ok_count}, ошибок: {fail_count}",
    }


async def _exec_global_presence_channel(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Создать каналы или группы для всех ожидающих целей плана global_presence."""
    from services import account_manager, session_simulator
    import random

    plan_id = params.get("plan_id")
    if not plan_id:
        return {"status": "failed", "reason": "Не указан plan_id"}

    # Получить тип актива (channel или group)
    plan = await pool.fetchrow("SELECT asset_type FROM global_presence_plans WHERE id=$1", plan_id)
    if not plan:
        return {"status": "failed", "reason": "План не найден"}

    asset_type = plan.get("asset_type", "channel")
    is_group = asset_type == "group"

    # Обновить статус плана на running
    await pool.execute(
        "UPDATE global_presence_plans SET status='running', updated_at=now() WHERE id=$1",
        plan_id,
    )

    targets = await pool.fetch(
        "SELECT * FROM global_presence_targets WHERE plan_id=$1 AND status='pending' ORDER BY id",
        plan_id,
    )
    if not targets:
        await pool.execute(
            "UPDATE global_presence_plans SET status='done', updated_at=now() WHERE id=$1", plan_id
        )
        return {"status": "done", "created": 0, "failed": 0, "summary": "Нет ожидающих целей"}

    # Загрузить аккаунты
    acc_ids = list({t["selected_account_id"] for t in targets if t["selected_account_id"]})
    if not acc_ids:
        return {"status": "failed", "reason": "Нет аккаунтов для выполнения"}

    accounts_rows = await pool.fetch(
        "SELECT a.id, a.session_str, a.phone, a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.id = ANY($1) AND a.is_active=true",
        acc_ids,
    )
    acc_by_id = {a["id"]: dict(a) for a in accounts_rows}

    created_count = 0
    failed_count = 0
    total = len(targets)

    for i, target in enumerate(targets):
        # Проверить отмену
        if await _is_cancelled(pool, op_id):
            await pool.execute(
                "UPDATE global_presence_plans SET status='cancelled', updated_at=now() WHERE id=$1", plan_id
            )
            return {
                "status": "cancelled",
                "created": created_count,
                "failed": failed_count,
                "summary": f"Отменено. Создано: {created_count}, ошибок: {failed_count}",
            }

        acc_id = target["selected_account_id"]
        acc = acc_by_id.get(acc_id)

        if not acc:
            await pool.execute(
                "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                "Аккаунт недоступен", target["id"],
            )
            failed_count += 1
            await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            continue

        # Пометить цель как running
        await pool.execute(
            "UPDATE global_presence_targets SET status='running' WHERE id=$1", target["id"]
        )

        title = target["planned_name"] or f"{'Group' if is_group else 'Channel'} {i + 1}"

        # Создать канал или группу
        result = await account_manager.create_channel(
            acc["session_str"], title, about="", megagroup=is_group, _acc=acc
        )

        # Обработка FloodWait — один повтор
        if result.get("error") and result.get("flood_wait"):
            wait_time = min(int(result["flood_wait"]) + 15, 300)
            log.info(
                "op_worker gp_%s: flood wait %ds for target %d",
                "group" if is_group else "channel",
                wait_time,
                target["id"],
            )
            await asyncio.sleep(wait_time)
            result = await account_manager.create_channel(
                acc["session_str"], title, about="", megagroup=is_group, _acc=acc
            )

        if result.get("error"):
            await pool.execute(
                "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                str(result["error"])[:500], target["id"],
            )
            failed_count += 1
            await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
            # После ошибки — небольшая пауза
            await asyncio.sleep(random.uniform(10, 25) * session_simulator.chaos_factor())
            continue

        channel_id = result.get("channel_id")

        # Установить username если задан
        username_error = None
        planned_username = target.get("planned_username")
        if planned_username and channel_id:
            await asyncio.sleep(random.uniform(3, 8))
            err = await account_manager.set_channel_username(
                acc["session_str"], channel_id, planned_username, _acc=acc
            )
            if err:
                # Попробовать варианты
                from services.username_engine import generate_username_variants
                geo = {
                    "country_code": target.get("country_code", ""),
                    "city": target.get("city", ""),
                    "city_slug": target.get("city_slug", ""),
                }
                variants = generate_username_variants(planned_username, geo)
                for variant in variants[1:4]:
                    await asyncio.sleep(random.uniform(2, 5))
                    err2 = await account_manager.set_channel_username(
                        acc["session_str"], channel_id, variant, _acc=acc
                    )
                    if not err2:
                        err = None
                        break
                username_error = err

        # Обновить цель как done
        await pool.execute(
            "UPDATE global_presence_targets SET status='done', result_asset_id=$1 WHERE id=$2",
            channel_id, target["id"],
        )
        created_count += 1

        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) VALUES($1,$2,$3,'ok',$4)",
            op_id, created_count + failed_count,
            f"{target.get('city', '?')} → {title}",
            f"channel_id={channel_id}" + (f" | username_err={username_error}" if username_error else ""),
        )
        await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)

        # Прогресс-уведомление каждые 10 созданных
        if created_count > 0 and created_count % 10 == 0:
            try:
                await bot.send_message(
                    owner_id,
                    f"🌍 <b>Создание каналов (план #{plan_id}):</b> {created_count + failed_count}/{total}\n"
                    f"✅ Создано: {created_count} | ❌ Ошибок: {failed_count}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Safe pacing: 45-90с между созданиями, каждые 5 — длинная пауза
        if i < total - 1:
            if i % 5 == 4:
                cooldown = random.uniform(300, 600) * session_simulator.chaos_factor()
                log.info("op_worker gp_channel: cooldown %.0fs after %d items", cooldown, i + 1)
                await asyncio.sleep(cooldown)
            else:
                delay = random.uniform(45, 90) * session_simulator.chaos_factor()
                await asyncio.sleep(delay)

    # Обновить статус плана
    final_status = "done" if failed_count == 0 else ("failed" if created_count == 0 else "done")
    await pool.execute(
        "UPDATE global_presence_plans SET status=$1, updated_at=now() WHERE id=$2",
        final_status, plan_id,
    )

    return {
        "status": "done",
        "created": created_count,
        "failed": failed_count,
        "plan_id": plan_id,
        "summary": f"Создано каналов: {created_count}, ошибок: {failed_count}",
    }
