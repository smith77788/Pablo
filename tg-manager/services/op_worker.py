"""Фоновый воркер для выполнения очереди операций (параллельный режим)."""
import asyncio
import json
import logging
import time
import aiohttp
import asyncpg
from aiogram import Bot
from database import db

log = logging.getLogger(__name__)
_POLL_INTERVAL = 10   # секунд между проверками очереди
_MAX_PARALLEL = 3     # максимум параллельных операций


async def _audit(
    pool: asyncpg.Pool,
    owner_id: int,
    action: str,
    result: str,
    operation_id: int | None = None,
    account_id: int | None = None,
    target: str | None = None,
    error_msg: str | None = None,
    flood_wait_s: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Записать событие в operation_audit. Никогда не бросает исключений."""
    try:
        await pool.execute(
            """INSERT INTO operation_audit(
                   owner_id, operation_id, account_id, action, target,
                   result, error_msg, flood_wait_s, duration_ms
               ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            owner_id, operation_id, account_id, action, target,
            result, error_msg, flood_wait_s, duration_ms,
        )
    except Exception as e:
        log.debug("audit write failed: %s", e)

_active_op_ids: set[int] = set()
_active_lock = asyncio.Lock()


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Запускается как asyncio.create_task(op_worker.run(pool, bot)) в main.py."""
    log.info("Operation worker started (parallel mode, max=%d)", _MAX_PARALLEL)
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
    async with _active_lock:
        available_slots = _MAX_PARALLEL - len(_active_op_ids)

    if available_slots <= 0:
        return

    # Атомарно захватить до available_slots pending-операций и перевести их в 'running'
    rows = await pool.fetch(
        """UPDATE operation_queue
           SET status = 'running', started_at = now()
           WHERE id IN (
               SELECT id FROM operation_queue
               WHERE status = 'pending'
                 AND (scheduled_for IS NULL OR scheduled_for <= now())
               ORDER BY created_at ASC
               LIMIT $1
               FOR UPDATE SKIP LOCKED
           )
           RETURNING id, owner_id, op_type, params""",
        available_slots,
    )

    for row in rows:
        op_id = row["id"]
        async with _active_lock:
            _active_op_ids.add(op_id)
        asyncio.create_task(_run_op_task(pool, bot, dict(row)))


async def _run_op_task(pool: asyncpg.Pool, bot: Bot, row: dict) -> None:
    """Запустить одну операцию в отдельной asyncio-задаче."""
    op_id = row["id"]
    owner_id = row["owner_id"]
    op_type = row["op_type"]
    params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"] or "{}")

    try:
        # Уведомить пользователя о старте
        try:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from bot.callbacks import BmCb
            start_kb = InlineKeyboardBuilder()
            start_kb.button(text="📋 Очередь операций", callback_data=BmCb(action="op_reports"))
            await bot.send_message(
                owner_id,
                f"⚙️ <b>Операция #{op_id}</b> запущена: <code>{op_type}</code>",
                parse_mode="HTML",
                reply_markup=start_kb.as_markup(),
            )
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

        # Не перезаписывать статус если операция была отменена в процессе
        if result.get("status") == "cancelled":
            return

        current = await pool.fetchrow("SELECT status FROM operation_queue WHERE id=$1", op_id)
        if current and current["status"] == "cancelled":
            return

        await pool.execute(
            "UPDATE operation_queue SET status='done', finished_at=now(), result=$1::jsonb WHERE id=$2",
            json.dumps(result), op_id,
        )
        summary = result.get("summary", "")
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
        await db.notify_if_enabled(
            pool, bot, owner_id, "op_complete",
            f"✅ <b>Операция #{op_id}</b> завершена\n{summary}",
            reply_markup=kb.as_markup(),
        )

    except Exception as e:
        log.exception("op_worker: op %d failed: %s", op_id, e)
        await pool.execute(
            "UPDATE operation_queue SET status='failed', finished_at=now(), error_msg=$1 WHERE id=$2",
            str(e)[:500], op_id,
        )
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
        await db.notify_if_enabled(
            pool, bot, owner_id, "op_complete",
            f"❌ <b>Операция #{op_id}</b> завершилась с ошибкой:\n<code>{str(e)[:200]}</code>",
            reply_markup=kb.as_markup(),
        )

    finally:
        async with _active_lock:
            _active_op_ids.discard(op_id)


async def _exec_mass_publish(pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict) -> dict:
    """Выполнить массовую публикацию."""
    from services import account_manager

    text = params.get("text", "")
    delay = params.get("delay_seconds", 30)
    account_ids = params.get("account_ids") or []

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
        if await _is_cancelled(pool, op_id):
            return {"status": "cancelled", "sent": total_sent, "failed": total_failed,
                    "summary": f"Отменено. Отправлено: {total_sent}, ошибок: {total_failed}"}
        acc_dict = dict(acc)
        try:
            dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc_dict)
            channels = [d for d in (dialogs or []) if d.get("type") in ("channel", "megagroup", "gigagroup")]

            for ch in channels:
                if await _is_cancelled(pool, op_id):
                    return {"status": "cancelled", "sent": total_sent, "failed": total_failed,
                            "summary": f"Отменено. Отправлено: {total_sent}, ошибок: {total_failed}"}
                try:
                    await account_manager.post_to_channel(
                        acc["session_str"], ch["id"], text, _acc=acc_dict
                    )
                    total_sent += 1
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
            if await _is_cancelled(pool, op_id):
                return {"status": "cancelled", "ok": ok_count, "failed": fail_count,
                        "summary": f"Отменено. Обновлено: {ok_count}, ошибок: {fail_count}"}
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
    from services import account_manager, session_simulator
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

    for acc in accounts:
        acc_dict = dict(acc)
        for i, link in enumerate(links):
            if await _is_cancelled(pool, op_id):
                return {"status": "cancelled", "ok": ok_count, "failed": fail_count,
                        "summary": f"Отменено. Вступлено: {ok_count}, ошибок: {fail_count}"}
            step += 1
            t0 = time.monotonic()
            try:
                res = await account_manager.join_channel(acc["session_str"], link, _acc=acc_dict)
                if res.get("error"):
                    raise Exception(res["error"])
                ok_count += 1
                dur_ms = int((time.monotonic() - t0) * 1000)
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'ok','joined')",
                    op_id, step, link,
                )
                await pool.execute(
                    "UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id
                )
                await _audit(pool, owner_id, "join", "success",
                             operation_id=op_id, account_id=acc["id"],
                             target=link, duration_ms=dur_ms)
                try:
                    from services.flood_engine import record_success
                    await record_success(acc["id"], "join")
                except Exception:
                    pass
            except Exception as e:
                fail_count += 1
                err_str = str(e)[:200]
                flood_wait = 0
                if "FloodWait" in err_str or "flood_wait" in err_str.lower():
                    try:
                        flood_wait = int(''.join(filter(str.isdigit, err_str.split("wait")[-1][:10])))
                    except Exception:
                        flood_wait = 60
                    try:
                        from services.flood_engine import record_flood
                        await record_flood(pool, acc["id"], flood_wait, "join", op_id)
                    except Exception:
                        pass
                await pool.execute(
                    "INSERT INTO operation_log(op_id, step_num, target, status, message) "
                    "VALUES($1,$2,$3,'error',$4)",
                    op_id, step, link, err_str,
                )
                await _audit(pool, owner_id, "join", "flood_wait" if flood_wait else "error",
                             operation_id=op_id, account_id=acc["id"],
                             target=link, error_msg=err_str, flood_wait_s=flood_wait or None)
            # Human-like anti-flood
            if i % 5 == 4:
                pause = random.uniform(180, 360) * session_simulator.chaos_factor()
            else:
                pause = random.uniform(45, 120) * session_simulator.chaos_factor()
            # Apply daily rhythm multiplier
            pause *= session_simulator.time_of_day_factor()
            await asyncio.sleep(pause)

    return {
        "status": "done",
        "ok": ok_count,
        "failed": fail_count,
        "summary": f"Вступлено: {ok_count}, ошибок: {fail_count}",
    }


async def _exec_bulk_leave(
    pool: asyncpg.Pool, bot: Bot, op_id: int, owner_id: int, params: dict
) -> dict:
    """Выйти из списка каналов/групп несколькими аккаунтами."""
    from services import account_manager, session_simulator
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

    for acc in accounts:
        acc_dict = dict(acc)
        for i, channel in enumerate(channels):
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
            # Human-like delay with daily rhythm
            pause = random.uniform(15, 45) * session_simulator.chaos_factor() * session_simulator.time_of_day_factor()
            await asyncio.sleep(pause)

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

    plan = await pool.fetchrow("SELECT asset_type FROM global_presence_plans WHERE id=$1", plan_id)
    if not plan:
        return {"status": "failed", "reason": "План не найден"}

    asset_type = plan.get("asset_type", "channel")
    is_group = asset_type == "group"

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

    acc_ids = list({t["selected_account_id"] for t in targets if t["selected_account_id"]})
    if not acc_ids:
        return {"status": "failed", "reason": "Нет аккаунтов для выполнения"}

    accounts_rows = await pool.fetch(
        "SELECT a.id, a.session_str, a.phone, a.device_model, a.system_version, a.app_version, a.trust_score, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.id = ANY($1) AND a.is_active=true "
        "ORDER BY a.trust_score DESC NULLS LAST",
        acc_ids,
    )
    acc_by_id = {a["id"]: dict(a) for a in accounts_rows}

    created_count = 0
    failed_count = 0
    total = len(targets)

    for i, target in enumerate(targets):
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

        # ── Проверка trust_score аккаунта перед использованием ──
        trust_score = acc.get("trust_score") or 0.5
        if trust_score < 0.3:
            log.warning(
                "op_worker gp_%s: skipping account %s with low trust_score=%.2f",
                "group" if is_group else "channel",
                acc["phone"], trust_score,
            )
            # Попробовать найти альтернативный аккаунт с лучшим trust_score
            alt_acc = None
            for a in accounts_rows:
                if a["id"] != acc_id and (a.get("trust_score") or 0.5) >= 0.5:
                    alt_acc = dict(a)
                    log.info("op_worker gp: switching to account %s with trust=%.2f", a["phone"], a.get("trust_score"))
                    break

            if not alt_acc:
                await pool.execute(
                    "UPDATE global_presence_targets SET status='failed', error_message=$1 WHERE id=$2",
                    f"Все аккаунты имеют низкий trust_score (мин: {trust_score:.2f})", target["id"],
                )
                failed_count += 1
                await pool.execute("UPDATE operation_queue SET done_items=done_items+1 WHERE id=$1", op_id)
                continue

            acc = alt_acc

        await pool.execute(
            "UPDATE global_presence_targets SET status='running' WHERE id=$1", target["id"]
        )

        title = target["planned_name"] or f"{'Group' if is_group else 'Channel'} {i + 1}"

        # ── Умная задержка перед созданием ──
        await session_simulator.typing_delay(title)  # 0.5-2с для натуральности

        result = await account_manager.create_channel(
            acc["session_str"], title, about="", megagroup=is_group, _acc=acc
        )

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
            await asyncio.sleep(random.uniform(10, 25) * session_simulator.chaos_factor())
            continue

        channel_id = result.get("channel_id")

        username_error = None
        planned_username = target.get("planned_username")
        if planned_username and channel_id:
            # ── Умная пауза перед установкой username ──
            pause = random.uniform(15, 25) * session_simulator.chaos_factor()
            await asyncio.sleep(pause)
            err = await account_manager.set_channel_username(
                acc["session_str"], channel_id, planned_username, _acc=acc
            )
            if err:
                log.info("op_worker gp_channel: username '%s' failed (%s), trying variants", planned_username, err[:80])
                if "flood" in err.lower() or "FloodWait" in err:
                    import re as _re
                    m = _re.search(r"(\d+)", err)
                    flood_wait = int(m.group(1)) + 5 if m else 60
                    log.info("op_worker gp_channel: FloodWait %ds, sleeping...", flood_wait)
                    await asyncio.sleep(flood_wait)
                from services.username_engine import generate_username_variants
                geo = {
                    "country_code": target.get("country_code", ""),
                    "city": target.get("city", ""),
                    "city_slug": target.get("city_slug", ""),
                }
                variants = generate_username_variants(planned_username, geo)
                for variant in variants[1:4]:
                    await asyncio.sleep(random.uniform(8, 15))
                    err2 = await account_manager.set_channel_username(
                        acc["session_str"], channel_id, variant, _acc=acc
                    )
                    if not err2:
                        log.info("op_worker gp_channel: username variant '%s' accepted", variant)
                        err = None
                        break
                    log.info("op_worker gp_channel: variant '%s' also failed: %s", variant, err2[:60])
                username_error = err

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

        if i < total - 1:
            # ── Почитай daily rhythm и избегай ночных часов пиков ──
            tod_factor = session_simulator.time_of_day_factor()  # 2-5x at night, 0.75x at peak
            chaos = session_simulator.chaos_factor()  # 0.7-1.3
            jitter = session_simulator.micro_jitter()  # ±10% микро-шум

            if i % 5 == 4:
                # Длинная пауза каждые 5 операций (имитация человеческого перерыва)
                cooldown = random.uniform(300, 600) * chaos * tod_factor * jitter
                log.info("op_worker gp_channel: cooldown %.0fs after %d items (tod_factor=%.2f)", cooldown, i + 1, tod_factor)
                await asyncio.sleep(cooldown)
            else:
                # Короткая пауза между операциями
                delay = random.uniform(45, 90) * chaos * tod_factor * jitter
                await asyncio.sleep(delay)

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
