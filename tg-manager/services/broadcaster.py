"""Background broadcast runner with rate-limiting and progress tracking.

Handles sending broadcast messages to multiple users with:
  - Rate limiting (configurable delay between messages)
  - Progress tracking and status updates
  - Template placeholder rendering
  - Content safety checks
  - Input validation and injection protection

Usage:
    from services.broadcaster import run, resume_interrupted

    # Resume broadcasts interrupted by previous restart
    await resume_interrupted(pool)

    # Start the background broadcast runner
    await run(pool, http, bot)
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime

import aiohttp
import asyncpg
from database import db
from services import bot_api
from services import brand_injection
from services import content_safety
from services.cache import TTLCache
from services.logger import log_exc_swallow
from services.security import (
    check_sql_suspicious,
    escape_html,
    validate_string,
    validate_bot_token,
)
from config import BROADCAST_DELAY

from bot.utils.template_validator import replace_placeholders

logger = logging.getLogger(__name__)

# broadcast_id → asyncio.Task, for optional cancellation
_running: dict[int, asyncio.Task] = {}

# Telegram group/channel rate limit: 20 messages/minute = 3 seconds between sends.
# Private users follow the global 30 msg/s limit (BROADCAST_DELAY covers that).
# Any chat_id < 0 is a group/channel.
_GROUP_DELAY = 3.0

# ── Кэш результатов рассылок (TTL 5 мин) ──────────────────────────────────────
_broadcast_result_cache = TTLCache(default_ttl=300.0, max_size=500)
# bot_id → timestamp — кэш проверки is_free_tier (TTL 10 мин)
_bot_tier_cache: dict[int, tuple[bool, float]] = {}
_BOT_TIER_TTL = 600.0
# Кэш user_map для placeholder-рендеринга по bot_id (TTL 2 мин)
_user_map_cache = TTLCache(default_ttl=120.0, max_size=2000)


def _render_for_user(text: str, user_info: dict, bot_name: str = "") -> str:
    """Render {{PLACEHOLDER}} tokens for a specific user."""
    if not text or "{{" not in text:
        return text
    username = user_info.get("username", "") or ""
    first_name = user_info.get("first_name", "") or ""
    last_name = user_info.get("last_name", "") or ""
    now = datetime.now()
    return replace_placeholders(
        text,
        {
            "USERNAME": f"@{username}" if username else first_name,
            "FIRST_NAME": first_name,
            "LAST_NAME": last_name,
            "FULL_NAME": f"{first_name} {last_name}".strip(),
            "BOT_NAME": bot_name,
            "DATE": now.strftime("%d.%m.%Y"),
            "DATE_SHORT": now.strftime("%d.%m"),
            "TIME": now.strftime("%H:%M"),
        },
    )


async def run(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession | None,
    broadcast_id: int,
    token: str,
    bot_id: int,
    text: str,
    photo_file_id: str | None = None,
    user_ids: list[int] | None = None,
    buttons: list[dict] | None = None,
    start_delay: float = 0.0,
    silent: bool = False,
) -> None:
    # ── Input validation ─────────────────────────────────────────────────────
    if broadcast_id <= 0:
        logger.error("Broadcast run: invalid broadcast_id=%d", broadcast_id)
        return
    if not validate_bot_token(token):
        logger.error("Broadcast %d: invalid token format", broadcast_id)
        try:
            await db.update_broadcast(pool, broadcast_id, 0, 0, "failed")
        except Exception:
            pass
        return
    if bot_id <= 0:
        logger.error("Broadcast %d: invalid bot_id=%d", broadcast_id, bot_id)
        try:
            await db.update_broadcast(pool, broadcast_id, 0, 0, "failed")
        except Exception:
            pass
        return
    text = validate_string(text, max_len=4096, required=True) or ""
    if not text:
        logger.warning("Broadcast %d: empty text, aborting", broadcast_id)
        try:
            await db.update_broadcast(pool, broadcast_id, 0, 0, "failed")
        except Exception:
            pass
        return
    if check_sql_suspicious(text):
        logger.warning("Broadcast %d: suspicious text detected, aborting", broadcast_id)
        try:
            await db.update_broadcast(pool, broadcast_id, 0, 0, "blocked")
        except Exception:
            pass
        return

    # ── Access control: verify bot ownership ──────────────────────────────────
    try:
        owner_check = await pool.fetchval(
            "SELECT added_by FROM managed_bots WHERE bot_id=$1 AND is_active=TRUE",
            bot_id,
        )
        if owner_check is None:
            logger.warning("Broadcast %d: bot %d not found or inactive", broadcast_id, bot_id)
            try:
                await db.update_broadcast(pool, broadcast_id, 0, 0, "failed")
            except Exception:
                pass
            return
    except Exception as _e:
        logger.warning("Broadcast %d: ownership check failed: %s", broadcast_id, _e)

    # Stagger start across multiple concurrent broadcasts (e.g. network broadcast)
    # so they don't all hammer Telegram at the same instant.
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    # If no session was provided (e.g. called from op_worker where the outer session
    # would already be closed by the time this task runs), create our own.
    _own_session = session is None
    if _own_session:
        session = aiohttp.ClientSession()

    if user_ids is None:
        user_ids = await db.get_audience_user_ids(pool, bot_id)

    # Content safety backstop: ни одна рассылка с запрещённым контентом
    # (CSAM / терроризм) не уходит подписчикам, даже если её создали в обход UI.
    _verdict = content_safety.scan_text(text)
    if _verdict.blocked:
        logger.warning(
            "Broadcast %d BLOCKED by content_safety: category=%s rule=%s",
            broadcast_id,
            _verdict.category,
            _verdict.rule,
        )
        try:
            await db.update_broadcast(pool, broadcast_id, 0, 0, "blocked")
        except Exception:
            try:
                await db.update_broadcast(pool, broadcast_id, 0, 0, "failed")
            except Exception as _e:
                logger.debug("Broadcast %d: could not mark blocked: %s", broadcast_id, _e)
        try:
            from services import compliance_engine

            await compliance_engine.record(
                pool, None, None,
                op_type="content_block:broadcast",
                outcome="blocked",
                op_id=broadcast_id,
                params={"category": _verdict.category, "rule": _verdict.rule},
            )
        except Exception as e:
            log_exc_swallow(log, "run")
        if _own_session and session is not None:
            await session.close()
        return

    # Pre-flight: verify token is valid before burning through 10k send attempts
    me = await bot_api.get_me(session, token)
    if not me:
        logger.error(
            "Broadcast %d: pre-flight getMe failed — token invalid or revoked; aborting",
            broadcast_id,
        )
        try:
            await db.update_broadcast(pool, broadcast_id, 0, 0, "failed")
        except Exception as _e:
            logger.warning("Broadcast %d: failed to mark failed: %s", broadcast_id, _e)
        return

    # Skip users already delivered (supports crash-resume without duplicate sends)
    try:
        already_sent: set[int] = await db.get_broadcast_delivered_ids(
            pool, broadcast_id
        )
    except Exception as _e:
        logger.warning(
            "Broadcast %d: could not load delivery log, starting fresh: %s",
            broadcast_id,
            _e,
        )
        already_sent = set()

    sent = len(already_sent)
    failed = 0
    # How often to flush progress to DB so the UI shows real-time progress.
    # 50 means every 50 successful sends we update sent_count in broadcasts table.
    _PROGRESS_FLUSH_INTERVAL = 50
    # Батч-логирование доставок: собираем user_ids и вставляем одним INSERT.
    # Это снижает количество round-trips к БД с 1 до ~N/50.
    _delivery_buffer: list[int] = []
    _DELIVERY_FLUSH_INTERVAL = 25
    _since_last_flush = 0
    try:
        await db.update_broadcast(pool, broadcast_id, sent, 0, "running")
    except Exception as _e:
        logger.warning("Broadcast %d: failed to mark running: %s", broadcast_id, _e)

    # Brand injection: append @MEXAHI3MBOT promo for free-tier bots (кэшировано)
    _now = time.monotonic()
    _tier_entry = _bot_tier_cache.get(bot_id)
    _is_free = (
        _tier_entry[0]
        if _tier_entry and (_now - _tier_entry[1]) < _BOT_TIER_TTL
        else None
    )
    if _is_free is None:
        try:
            _is_free = await brand_injection.is_free_tier(pool, bot_id)
            _bot_tier_cache[bot_id] = (_is_free, _now)
        except Exception as _bi_err:
            _is_free = False
            logger.debug("Broadcast %d: brand_injection check failed: %s", broadcast_id, _bi_err)
    if _is_free:
        text = brand_injection.add_promo(text, html=True, context="broadcast")

    # Pre-load user data for placeholder rendering if needed (кэшировано по bot_id)
    has_placeholders = "{{" in text
    user_map: dict[int, dict] = {}
    if has_placeholders and user_ids:
        _um_cache_key = f"um:{bot_id}"
        _um_cached = _user_map_cache.get(_um_cache_key)
        if _um_cached is not None:
            # Фильтруем только нужных user_ids из кэша
            user_map = {uid: _um_cached[uid] for uid in user_ids if uid in _um_cached}
        if not user_map:
            rows = await pool.fetch(
                "SELECT user_id, username, first_name, last_name FROM bot_users "
                "WHERE bot_id=$1 AND user_id = ANY($2::bigint[])",
                bot_id,
                user_ids,
            )
            user_map = {r["user_id"]: dict(r) for r in rows}
            # Кэшируем полный набор пользователей бота
            _user_map_cache.set(_um_cache_key, user_map)
    bot_name = ""
    if has_placeholders:
        _bn_key = f"bn:{bot_id}"
        _bn_cached = _broadcast_result_cache.get(_bn_key)
        if _bn_cached is not None:
            bot_name = _bn_cached
        else:
            bot_row = await pool.fetchrow(
                "SELECT username, first_name FROM managed_bots WHERE bot_id=$1", bot_id
            )
            if bot_row:
                bot_name = bot_row.get("username") or bot_row.get("first_name") or ""
            _broadcast_result_cache.set(_bn_key, bot_name)
    user_count = len(user_ids)

    # Вспомогательная функция: батч-сброс буфера доставки в БД
    async def _flush_delivery_buffer() -> None:
        nonlocal _delivery_buffer
        if not _delivery_buffer:
            return
        batch = _delivery_buffer[:]
        _delivery_buffer.clear()
        try:
            await pool.executemany(
                "INSERT INTO broadcast_delivery_log (broadcast_id, user_id) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                [(broadcast_id, uid) for uid in batch],
            )
        except Exception as _e:
            logger.warning(
                "Broadcast %d: batch delivery log failed (%d users): %s",
                broadcast_id, len(batch), _e,
            )

    # _loop_exc captures any exception (including CancelledError) so we can
    # always mark the broadcast final status in DB before propagating.
    _loop_exc: BaseException | None = None
    try:
        for uid in user_ids:
            # Resume support: skip users already reached in a previous run
            if uid in already_sent:
                continue

            # Render per-user placeholders
            user_text = text
            if has_placeholders:
                ui = user_map.get(uid, {})
                user_text = _render_for_user(text, ui, bot_name)

            if photo_file_id:
                success, retry_after = await bot_api.send_photo(
                    session, token, uid, photo_file_id, user_text, buttons=buttons, disable_notification=silent
                )
            else:
                success, retry_after = await bot_api.send_message(
                    session, token, uid, user_text, buttons=buttons, disable_notification=silent
                )
            if success:
                sent += 1
                _since_last_flush += 1
                _delivery_buffer.append(uid)
                # Периодический батч-сброс доставок + прогресса в БД
                if len(_delivery_buffer) >= _DELIVERY_FLUSH_INTERVAL:
                    await _flush_delivery_buffer()
                if _since_last_flush >= _PROGRESS_FLUSH_INTERVAL:
                    _since_last_flush = 0
                    try:
                        await db.update_broadcast(pool, broadcast_id, sent, failed, "running")
                    except Exception as _fe:
                        logger.debug(
                            "Broadcast %d: progress flush failed (non-fatal): %s",
                            broadcast_id, _fe,
                        )
            else:
                failed += 1
                if retry_after:
                    logger.info(
                        "Broadcast %d: rate-limited, sleeping %ds",
                        broadcast_id, retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    if photo_file_id:
                        ok, _ = await bot_api.send_photo(
                            session, token, uid, photo_file_id, user_text, buttons=buttons, disable_notification=silent
                        )
                    else:
                        ok, _ = await bot_api.send_message(
                            session, token, uid, user_text, buttons=buttons, disable_notification=silent
                        )
                    if ok:
                        sent += 1
                        failed -= 1
                        _since_last_flush += 1
                        _delivery_buffer.append(uid)
                        if len(_delivery_buffer) >= _DELIVERY_FLUSH_INTERVAL:
                            await _flush_delivery_buffer()
                    else:
                        await db.mark_user_inactive(pool, bot_id, uid)
                else:
                    await db.mark_user_inactive(pool, bot_id, uid)

            # Respect per-chat type rate limits.
            # Groups/channels: 20 msg/min max → 3s between sends to same chat.
            # Private users: global 30 msg/s limit → BROADCAST_DELAY (default 0.05s).
            delay = _GROUP_DELAY if uid < 0 else BROADCAST_DELAY
            await asyncio.sleep(delay)
    except BaseException as _exc:
        _loop_exc = _exc
        if not isinstance(_exc, asyncio.CancelledError):
            logger.error(
                "Broadcast %d: send loop aborted after %d sent: %s",
                broadcast_id,
                sent,
                _exc,
                exc_info=True,
            )

    # Финальный сброс оставшихся доставок из буфера
    await _flush_delivery_buffer()

    total = user_count
    if _loop_exc is not None:
        final_status = "partial" if sent > 0 else "failed"
    elif total == 0 or sent == total:
        final_status = "done"
    elif sent == 0:
        final_status = "failed"
    else:
        final_status = "partial"

    try:
        await db.update_broadcast(pool, broadcast_id, sent, failed, final_status)
    except Exception as _e:
        logger.warning(
            "Broadcast %d: failed to mark %s: %s", broadcast_id, final_status, _e
        )
    finally:
        _running.pop(broadcast_id, None)
        if _own_session:
            await session.close()

    if _loop_exc is not None:
        raise _loop_exc
    logger.info(
        "Broadcast %d %s: sent=%d failed=%d total=%d",
        broadcast_id,
        final_status,
        sent,
        failed,
        total,
    )


def _on_broadcast_done(broadcast_id: int, task: asyncio.Task) -> None:
    """Log unhandled exceptions from broadcast tasks so they aren't silently swallowed."""
    _running.pop(broadcast_id, None)
    exc = task.exception() if not task.cancelled() else None
    if exc:
        logger.error(
            "Broadcast %d raised unhandled exception: %s",
            broadcast_id,
            exc,
            exc_info=exc,
        )


def start(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession | None,
    broadcast_id: int,
    token: str,
    bot_id: int,
    text: str,
    photo_file_id: str | None = None,
    user_ids: list[int] | None = None,
    buttons: list[dict] | None = None,
    start_delay: float = 0.0,
    silent: bool = False,
) -> None:
    task = asyncio.create_task(
        run(
            pool,
            session,
            broadcast_id,
            token,
            bot_id,
            text,
            photo_file_id,
            user_ids,
            buttons,
            start_delay,
            silent,
        ),
        name=f"broadcast-{broadcast_id}",
    )
    _running[broadcast_id] = task
    task.add_done_callback(lambda t: _on_broadcast_done(broadcast_id, t))


async def resume_interrupted(
    pool: asyncpg.Pool, session: aiohttp.ClientSession | None = None
) -> None:
    """Перезапустить рассылки, оборвавшиеся на рестарте процесса.

    После рестарта in-memory _running пуст, а в БД остаются рассылки в статусе
    running/pending, чьи asyncio-задачи умерли. broadcaster.run докатывает их,
    пропуская уже доставленных через delivery log (без дублей). Сегментные
    рассылки используют сохранённый target_user_ids, полные — всю аудиторию.
    """
    try:
        rows = await db.get_interrupted_broadcasts(pool)
    except Exception as exc:
        logger.warning("resume_interrupted: не удалось загрузить рассылки: %s", exc)
        return
    if not rows:
        return
    logger.info("resume_interrupted: перезапуск %d прерванных рассылок", len(rows))
    for i, r in enumerate(rows):
        try:
            start(
                pool,
                session,
                r["id"],
                r["token"],
                r["bot_id"],
                r["message_text"] or "",
                r.get("photo_file_id"),
                r.get("target_user_ids"),  # список (сегмент) или None (полная аудитория)
                r.get("buttons"),  # восстановить инлайн-кнопки после рестарта
                start_delay=i * 2.0,  # разносим старты, чтобы не бить по Telegram разом
                silent=bool(r.get("silent")),
            )
        except Exception as exc:
            logger.warning("resume_interrupted: рассылка %s не перезапущена: %s", r.get("id"), exc)


def cancel(broadcast_id: int) -> bool:
    task = _running.get(broadcast_id)
    if task and not task.done():
        task.cancel()
        _running.pop(broadcast_id, None)
        return True
    return False


def is_running(broadcast_id: int) -> bool:
    task = _running.get(broadcast_id)
    return task is not None and not task.done()


async def mass_broadcast_with_scheduling(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    text: str,
    schedule: dict,
) -> dict:
    """Создать рассылку с расписанием (отложенная отправка).

    schedule = {"schedule_minutes": int} | {"scheduled_for": "ISO datetime"}.
    Возвращает {"ok": True, "broadcast_id": int, "op_id": int} или ошибку.
    """
    import json as _json

    text = validate_string(text, max_len=4096, required=True) or ""
    if not text:
        return {"ok": False, "error": "Empty message text"}
    if check_sql_suspicious(text):
        return {"ok": False, "error": "Invalid characters in message text"}

    bot_row = await pool.fetchrow(
        "SELECT bot_id, token, username FROM managed_bots "
        "WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id, owner_id,
    )
    if not bot_row:
        return {"ok": False, "error": "Bot not found"}

    segment = (schedule.get("segment") or "all")
    _seg_sql = {
        "active_7d": " AND last_seen >= now() - interval '7 days'",
        "active_30d": " AND last_seen >= now() - interval '30 days'",
    }.get(segment, "")

    # safe_count: сегментный фильтр по last_seen может ссылаться на колонку,
    # которой ещё нет при миграционном лаге — тогда COUNT падает UndefinedColumn.
    # db.safe_count глотает сбой БД → 0, рассылка не падает целиком.
    total = await db.safe_count(
        pool,
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true" + _seg_sql,
        bot_id,
    )

    buttons = []
    for b in (schedule.get("buttons") or [])[:10]:
        try:
            bt = validate_string(b.get("text"), max_len=64) or ""
            bu = validate_string(b.get("url"), max_len=2048) or ""
        except Exception:
            continue
        if bt and bu.lower().startswith(("http://", "https://")):
            if check_sql_suspicious(bu):
                logger.warning("mass_broadcast: suspicious button URL blocked")
                continue
            buttons.append({"text": escape_html(bt), "url": bu})

    schedule_minutes = max(0, min(int(schedule.get("schedule_minutes") or 0), 60 * 24 * 30))
    scheduled_for_iso = schedule.get("scheduled_for")

    bot_label = bot_row.get("username") or bot_id
    label = f"Рассылка боту @{bot_label}: {text[:40]}…" if len(text) > 40 else f"Рассылка: {text[:60]}"

    _op_params: dict = {"bot_id": bot_id, "text": text}
    if buttons:
        _op_params["buttons"] = buttons
    if _seg_sql:
        _op_params["segment"] = segment

    broadcast_id = None
    if schedule_minutes <= 0 and not scheduled_for_iso:
        try:
            row = await pool.fetchrow(
                "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, buttons) "
                "VALUES($1,$2,$3,'pending',$4,$5::jsonb) RETURNING id",
                bot_id, text, total, owner_id, _json.dumps(buttons) if buttons else None,
            )
            broadcast_id = row["id"]
            _op_params["broadcast_id"] = broadcast_id
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'run_broadcast','pending',$2,$3,$4) RETURNING id",
                owner_id, _json.dumps(_op_params), total, label,
            )
        except Exception as exc:
            logger.exception("mass_broadcast_with_scheduling insert uid=%d", owner_id)
            return {"ok": False, "error": str(exc)[:200]}
    else:
        label = f"⏰ {label}"
        if scheduled_for_iso:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label, scheduled_for) "
                "VALUES($1,'run_broadcast','pending',$2,$3,$4,$5::timestamptz) RETURNING id",
                owner_id, _json.dumps(_op_params), total, label, scheduled_for_iso,
            )
        else:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label, scheduled_for) "
                "VALUES($1,'run_broadcast','pending',$2,$3,$4, now() + ($5 || ' minutes')::interval) RETURNING id",
                owner_id, _json.dumps(_op_params), total, label, str(schedule_minutes),
            )

    return {
        "ok": True,
        "broadcast_id": broadcast_id,
        "op_id": op_id,
        "total_users": total,
        "scheduled_minutes": schedule_minutes,
    }


async def resend_undelivered(pool: asyncpg.Pool, owner_id: int, bc_id: int) -> dict:
    """Повторная рассылка ТОЛЬКО недоставленным получателям исходной рассылки.

    Недоставленные = активные подписчики бота, которых нет в broadcast_delivery_log
    исходной рассылки. Единая реализация для mini-app и бота. Скоупится по
    created_by/added_by (владелец). Возвращает {ok, broadcast_id, op_id, total_users}
    или {ok: False, error, code}.
    """
    async def _sfrow(q, *a):
        try:
            r = await pool.fetchrow(q, *a)
            return dict(r) if r else None
        except Exception as e:
            logger.warning("resend_undelivered fetchrow: %s", e)
            return None

    src = await _sfrow(
        "SELECT id, bot_id, message_text, created_by FROM broadcasts WHERE id=$1",
        bc_id,
    )
    if not src or int(src.get("created_by") or 0) != owner_id:
        return {"ok": False, "error": "Рассылка не найдена", "code": 404}
    bot_id_int = int(src["bot_id"])
    bot_row = await _sfrow(
        "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id_int, owner_id,
    )
    if not bot_row:
        return {"ok": False, "error": "Бот не найден", "code": 404}
    text = (src.get("message_text") or "").strip()
    if not text:
        return {"ok": False, "error": "У исходной рассылки нет текста", "code": 400}
    try:
        rows = await pool.fetch(
            """SELECT bu.user_id FROM bot_users bu
               WHERE bu.bot_id=$1 AND bu.is_active=true
                 AND NOT EXISTS (
                     SELECT 1 FROM broadcast_delivery_log dl
                     WHERE dl.broadcast_id=$2 AND dl.user_id=bu.user_id)""",
            bot_id_int, bc_id,
        )
    except Exception as e:
        logger.warning("resend_undelivered fetch undelivered: %s", e)
        rows = []
    undelivered = [int(r["user_id"]) for r in (rows or [])]
    if not undelivered:
        return {"ok": False,
                "error": "Все активные подписчики уже получили рассылку", "code": 400}
    total = len(undelivered)
    import json as _json
    try:
        row = await pool.fetchrow(
            "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
            "VALUES($1,$2,$3,'pending',$4) RETURNING id",
            bot_id_int, text, total, owner_id)
        new_bc = row["id"]
        label = (f"Повтор недоставленным: {text[:40]}…"
                 if len(text) > 40 else f"Повтор: {text[:60]}")
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,'run_broadcast','pending',$2,$3,$4) RETURNING id",
            owner_id, _json.dumps({"bot_id": bot_id_int, "broadcast_id": new_bc,
                                   "text": text, "user_ids": undelivered}),
            total, label)
        return {"ok": True, "broadcast_id": new_bc, "op_id": op_id, "total_users": total}
    except Exception:
        logger.exception("resend_undelivered bc=%d uid=%d", bc_id, owner_id)
        return {"ok": False, "error": "Не удалось создать повторную рассылку", "code": 500}


async def ab_test_broadcast(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    variants: list[dict],
) -> dict:
    """A/B тестирование рассылок.

    variants = [{"text": str, "weight": int}, ...]. Каждый вариант — отдельная
    рассылка, аудитория делится пропорционально weight.
    Возвращает {"ok": True, "broadcasts": [{"variant_index": int, "broadcast_id": int, ...}]}.
    """
    import json as _json
    import random as _rand

    if not variants or not isinstance(variants, list):
        return {"ok": False, "error": "variants required (list of {text, weight})"}
    if len(variants) > 10:
        return {"ok": False, "error": "Max 10 variants"}

    bot_row = await pool.fetchrow(
        "SELECT bot_id, token, username FROM managed_bots "
        "WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id, owner_id,
    )
    if not bot_row:
        return {"ok": False, "error": "Bot not found"}

    user_ids = await db.get_audience_user_ids(pool, bot_id)
    if not user_ids:
        return {"ok": False, "error": "No active subscribers"}

    total_weight = sum(max(1, v.get("weight", 1)) for v in variants)
    _rand.shuffle(user_ids)
    chunks: list[list[int]] = []
    offset = 0
    for v in variants:
        w = max(1, v.get("weight", 1))
        size = max(1, int(len(user_ids) * w / total_weight))
        chunks.append(user_ids[offset:offset + size])
        offset += size
    if offset < len(user_ids):
        chunks[-1].extend(user_ids[offset:])

    results = []
    for idx, (v, chunk) in enumerate(zip(variants, chunks)):
        text = (v.get("text") or "").strip()
        if not text or not chunk:
            continue
        if check_sql_suspicious(text):
            continue
        text = validate_string(text, max_len=4096) or ""
        if not text:
            continue

        try:
            row = await pool.fetchrow(
                "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
                "VALUES($1,$2,$3,'pending',$4) RETURNING id",
                bot_id, text, len(chunk), owner_id,
            )
            bc_id = row["id"]
            _op_params = {"bot_id": bot_id, "text": text, "broadcast_id": bc_id, "user_ids": chunk}
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'run_broadcast','pending',$2,$3,$4) RETURNING id",
                owner_id, _json.dumps(_op_params), len(chunk),
                f"A/B вариант {idx + 1}: {text[:30]}",
            )
            results.append({"variant_index": idx, "broadcast_id": bc_id, "op_id": op_id, "users": len(chunk)})
        except Exception as exc:
            logger.warning("ab_test_broadcast variant %d failed: %s", idx, exc)

    return {"ok": True, "broadcasts": results, "total_users": len(user_ids)}


async def get_broadcast_analytics(
    pool: asyncpg.Pool,
    owner_id: int,
    broadcast_id: int,
) -> dict:
    """Аналитика рассылки: статус, доставка, клики, ошибка.

    Возвращает сводку по рассылке с проверкой владения.
    """
    if broadcast_id <= 0 or owner_id <= 0:
        return {"ok": False, "error": "Invalid parameters"}
    row = await pool.fetchrow(
        """SELECT b.id, b.bot_id, b.message_text, b.status,
                  b.sent_count, b.failed_count, b.total_users, b.created_at,
                  b.buttons, b.silent,
                  mb.username AS bot_username
           FROM broadcasts b
           JOIN managed_bots mb ON mb.bot_id = b.bot_id
           WHERE b.id = $1 AND b.created_by = $2""",
        broadcast_id, owner_id,
    )
    if not row:
        return {"ok": False, "error": "Broadcast not found"}

    delivery_log = await pool.fetch(
        "SELECT user_id, sent_at FROM broadcast_delivery_log WHERE broadcast_id = $1 "
        "ORDER BY sent_at LIMIT 1000",
        broadcast_id,
    )

    sent = int(row.get("sent_count") or 0)
    failed = int(row.get("failed_count") or 0)
    total = int(row.get("total_users") or 0)
    delivery_rate = round(sent / total * 100, 1) if total > 0 else 0

    hourly: dict[str, int] = {}
    for d in delivery_log:
        ts = d.get("sent_at")
        if ts:
            key = ts.strftime("%H:00") if hasattr(ts, "strftime") else str(ts)[:13]
            hourly[key] = hourly.get(key, 0) + 1

    return {
        "ok": True,
        "broadcast_id": broadcast_id,
        "bot_id": row["bot_id"],
        "bot_username": row.get("bot_username"),
        "message_text": row.get("message_text"),
        "status": row.get("status"),
        "sent_count": sent,
        "failed_count": failed,
        "total_users": total,
        "delivery_rate_pct": delivery_rate,
        "has_buttons": bool(row.get("buttons")),
        "silent": bool(row.get("silent")),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "delivery_hourly": hourly,
    }
