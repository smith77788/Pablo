"""Background task: запускает запланированные growth-посты каждые 10 минут."""
from __future__ import annotations

import asyncio
import logging

import asyncpg
from aiogram import Bot

from database import db
from services import account_manager

log = logging.getLogger(__name__)

_INTERVAL = 600  # 10 минут


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    while True:
        try:
            await _tick(pool, bot)
        except Exception as exc:
            log.exception("growth_scheduler tick error: %s", exc)
        await asyncio.sleep(_INTERVAL)


async def _tick(pool: asyncpg.Pool, bot: Bot) -> None:
    try:
        schedules = await db.get_due_growth_schedules(pool)
    except Exception:
        return  # таблицы ещё не созданы

    for sched in schedules:
        try:
            await _fire(pool, bot, sched)
        except Exception as exc:
            log.warning("growth_scheduler fire sched=%s: %s", sched["id"], exc)


async def _fire(pool: asyncpg.Pool, bot: Bot, sched: dict) -> None:
    user_id = sched["user_id"]
    channels = await db.get_managed_channels(pool, user_id)
    if not channels:
        await db.advance_growth_schedule(pool, sched["id"], sched["interval_h"], 0)
        return

    # Подставляем реальную статистику в шаблон
    try:
        stats = await db.get_growth_platform_stats(pool)
        me = await bot.get_me()
        code = await db.get_or_create_referral_code(pool, user_id)
        ref_link = f"https://t.me/{me.username}?start={code}"
    except Exception:
        ref_link = "https://t.me/BotMotherBot"
        stats = {"total_users": 0, "total_ops": 0, "total_channels": 0, "week_users": 0}

    content = sched["template"].format(
        users=f"{int(stats['total_users']):,}".replace(",", " "),
        ops=f"{int(stats['total_ops']):,}".replace(",", " "),
        channels=f"{int(stats['total_channels']):,}".replace(",", " "),
        ref_link=ref_link,
    )

    # Watermark если включён
    try:
        settings = await db.get_growth_settings(pool, user_id)
        if settings.get("watermark_enabled"):
            content += f'\n\n📤 <a href="{ref_link}">BotMother</a>'
    except Exception:
        pass

    sent = failed = 0
    for ch in channels:
        try:
            acc_row = await db.get_account_for_telethon(pool, ch["acc_id"], user_id)
            if not acc_row or not acc_row["session_str"]:
                failed += 1
                continue
            res = await account_manager.post_to_channel(
                session_string=acc_row["session_str"],
                channel_id=ch["channel_id"],
                text=content,
                access_hash=ch.get("access_hash") or 0,
                _acc=dict(acc_row),
            )
            if res.get("error"):
                failed += 1
            else:
                sent += 1
        except Exception as exc:
            log.warning("growth_scheduler channel=%s: %s", ch.get("channel_id"), exc)
            failed += 1
        await asyncio.sleep(1.5)

    await db.advance_growth_schedule(pool, sched["id"], sched["interval_h"], sent)
    log.info("growth_scheduler sched=%s fired: sent=%d failed=%d", sched["id"], sent, failed)

    if sent > 0:
        try:
            await bot.send_message(
                user_id,
                f"🌱 <b>Авто-пост опубликован!</b>\n\n"
                f"📢 Каналов: <b>{sent}</b> ⚠️ Ошибок: <b>{failed}</b>\n"
                f"Следующий запуск через <b>{sched['interval_h']}ч</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
