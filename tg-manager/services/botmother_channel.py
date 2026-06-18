"""Публикация обновлений и промо-постов в официальный канал BotMother.

Бот должен быть администратором канала с правами публикации.
ID канала хранится в platform_settings['botmother_channel_id'].
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

_SETTING_KEY = "botmother_channel_id"


async def get_channel_id(pool: asyncpg.Pool) -> str | None:
    """Вернуть ID/username канала из настроек платформы."""
    try:
        row = await pool.fetchrow(
            "SELECT value FROM platform_settings WHERE key=$1", _SETTING_KEY
        )
        v = row["value"].strip() if row else ""
        return v if v else None
    except Exception as e:
        log.debug("botmother_channel.get_channel_id: %s", e)
        return None


async def set_channel_id(pool: asyncpg.Pool, channel_id: str) -> None:
    """Сохранить ID/username канала в настройках платформы."""
    await pool.execute(
        """INSERT INTO platform_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()""",
        _SETTING_KEY,
        channel_id.strip(),
    )


async def post(pool: asyncpg.Pool, bot: Bot, text: str) -> bool:
    """Опубликовать текст в BotMother канал. Возвращает True при успехе."""
    channel_id = await get_channel_id(pool)
    if not channel_id:
        log.warning("botmother_channel.post: channel_id не настроен")
        return False
    try:
        await bot.send_message(
            chat_id=channel_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        log.warning("botmother_channel.post to %s: %s", channel_id, e)
        return False


async def post_changelog(
    pool: asyncpg.Pool,
    bot: Bot,
    title: str,
    changes: list[str],
    version: str = "",
) -> bool:
    """Публикует отформатированный changelog-пост в канал."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d.%m.%Y")
    ver_str = f" <code>{version}</code>" if version else ""

    bullets = "\n".join(f"• {c}" for c in changes)
    text = (
        f"🔧 <b>{title}</b>{ver_str}\n"
        f"<i>{date_str}</i>\n\n"
        f"{bullets}\n\n"
        f"📊 Ваша инфраструктура Telegram работает на BotMother\n"
        f"👉 @MEXAHI3MBOT"
    )
    return await post(pool, bot, text)


async def post_stats_update(pool: asyncpg.Pool, bot: Bot) -> bool:
    """Публикует пост со статистикой роста платформы."""
    try:
        row = await pool.fetchrow(
            """SELECT
                (SELECT COUNT(*) FROM platform_users) AS users,
                (SELECT COUNT(*) FROM managed_bots WHERE is_active=TRUE) AS bots,
                (SELECT COUNT(*) FROM managed_channels) AS channels,
                (SELECT COUNT(*) FROM operation_queue WHERE created_at >= NOW() - INTERVAL '7 days') AS weekly_ops
            """
        )
    except Exception:
        row = None

    users = int(row["users"] or 0) if row else 0
    bots = int(row["bots"] or 0) if row else 0
    channels = int(row["channels"] or 0) if row else 0
    weekly_ops = int(row["weekly_ops"] or 0) if row else 0

    text = (
        f"📊 <b>BotMother — статистика платформы</b>\n\n"
        f"👥 Пользователей: <b>{users:,}".replace(",", " ") + f"</b>\n"
        f"🤖 Активных ботов: <b>{bots:,}".replace(",", " ") + f"</b>\n"
        f"📢 Каналов в управлении: <b>{channels:,}".replace(",", " ") + f"</b>\n"
        f"⚡ Операций за неделю: <b>{weekly_ops:,}".replace(",", " ") + f"</b>\n\n"
        f"🚀 Присоединяйся — автоматизируй свой Telegram\n"
        f"👉 @MEXAHI3MBOT"
    )
    return await post(pool, bot, text)


async def post_promo_offer(pool: asyncpg.Pool, bot: Bot) -> bool:
    """Публикует рекламное предложение в канал."""
    try:
        row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM platform_users"
        )
        users = int(row["cnt"] or 0) if row else 0
    except Exception:
        users = 0

    text = (
        f"📣 <b>Реклама в BotMother</b>\n\n"
        f"Наш бот используют <b>{users:,}".replace(",", " ") + f"</b> владельцев Telegram-каналов и ботов.\n\n"
        f"<b>Аудитория:</b>\n"
        f"• Владельцы каналов и ботов\n"
        f"• Маркетологи и предприниматели\n"
        f"• Специалисты по Telegram-автоматизации\n\n"
        f"<b>Форматы рекламы:</b>\n"
        f"📌 Пост в канале\n"
        f"🤖 Упоминание в боте\n"
        f"📨 DM-кампания по нашей базе\n\n"
        f"💬 Для сотрудничества: @MEXAHI3MBOT\n\n"
        f"<i>BotMother — Telegram OS для роста вашей инфраструктуры</i>"
    )
    return await post(pool, bot, text)
