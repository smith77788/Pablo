"""Background broadcast runner with rate-limiting and progress tracking."""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime

import aiohttp
import asyncpg
from database import db
from services import bot_api
from services import brand_injection
from services import content_safety
from config import BROADCAST_DELAY

from bot.utils.template_validator import replace_placeholders

logger = logging.getLogger(__name__)

# broadcast_id → asyncio.Task, for optional cancellation
_running: dict[int, asyncio.Task] = {}

# Telegram group/channel rate limit: 20 messages/minute = 3 seconds between sends.
# Private users follow the global 30 msg/s limit (BROADCAST_DELAY covers that).
# Any chat_id < 0 is a group/channel.
_GROUP_DELAY = 3.0


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
) -> None:
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
        except Exception:
            pass
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
    _since_last_flush = 0
    try:
        await db.update_broadcast(pool, broadcast_id, sent, 0, "running")
    except Exception as _e:
        logger.warning("Broadcast %d: failed to mark running: %s", broadcast_id, _e)

    # Brand injection: append @MEXAHI3MBOT promo for free-tier bots
    try:
        if await brand_injection.is_free_tier(pool, bot_id):
            text = brand_injection.add_promo(text, html=True, context="broadcast")
    except Exception as _bi_err:
        logger.debug("Broadcast %d: brand_injection check failed: %s", broadcast_id, _bi_err)

    # Pre-load user data for placeholder rendering if needed
    has_placeholders = "{{" in text
    user_map: dict[int, dict] = {}
    if has_placeholders and user_ids:
        rows = await pool.fetch(
            "SELECT user_id, username, first_name, last_name FROM bot_users "
            "WHERE bot_id=$1 AND user_id = ANY($2::bigint[])",
            bot_id,
            user_ids,
        )
        user_map = {r["user_id"]: dict(r) for r in rows}
    bot_name = ""
    if has_placeholders:
        bot_row = await pool.fetchrow(
            "SELECT username, first_name FROM managed_bots WHERE bot_id=$1", bot_id
        )
        if bot_row:
            bot_name = bot_row.get("username") or bot_row.get("first_name") or ""
    user_count = len(user_ids)

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
                    session, token, uid, photo_file_id, user_text, buttons=buttons
                )
            else:
                success, retry_after = await bot_api.send_message(
                    session, token, uid, user_text, buttons=buttons
                )
            if success:
                sent += 1
                _since_last_flush += 1
                # Log delivery immediately so a crash can resume from here
                try:
                    await db.log_broadcast_delivery(pool, broadcast_id, uid)
                except Exception as _e:
                    logger.warning(
                        "Broadcast %d: failed to log delivery for user %d: %s",
                        broadcast_id,
                        uid,
                        _e,
                    )
                # Periodically flush progress to DB so the broadcast history shows
                # real-time progress instead of staying at 0/N until completion.
                if _since_last_flush >= _PROGRESS_FLUSH_INTERVAL:
                    _since_last_flush = 0
                    try:
                        await db.update_broadcast(pool, broadcast_id, sent, failed, "running")
                    except Exception as _fe:
                        logger.debug(
                            "Broadcast %d: progress flush failed (non-fatal): %s",
                            broadcast_id,
                            _fe,
                        )
            else:
                failed += 1
                if retry_after:
                    logger.info(
                        "Broadcast %d: rate-limited, sleeping %ds",
                        broadcast_id,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    if photo_file_id:
                        ok, _ = await bot_api.send_photo(
                            session, token, uid, photo_file_id, user_text, buttons=buttons
                        )
                    else:
                        ok, _ = await bot_api.send_message(
                            session, token, uid, user_text, buttons=buttons
                        )
                    if ok:
                        sent += 1
                        failed -= 1
                        _since_last_flush += 1
                        try:
                            await db.log_broadcast_delivery(pool, broadcast_id, uid)
                        except Exception as _e:
                            logger.warning(
                                "Broadcast %d: failed to log delivery for user %d: %s",
                                broadcast_id,
                                uid,
                                _e,
                            )
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
