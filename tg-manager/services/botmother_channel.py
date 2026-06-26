"""Публикация обновлений и промо-постов в официальный канал Infragram.

Бот должен быть администратором канала с правами публикации.
ID канала хранится в platform_settings['botmother_channel_id'].
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

_SETTING_KEY = "botmother_channel_id"


async def get_channel_id(pool: asyncpg.Pool) -> str | None:
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
    await pool.execute(
        """INSERT INTO platform_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()""",
        _SETTING_KEY,
        channel_id.strip(),
    )


async def post(pool: asyncpg.Pool, bot: Bot, text: str) -> bool:
    """Опубликовать текст в Infragram канал. Возвращает True при успехе."""
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
        f"📊 Управляй своей Telegram-инфраструктурой на автопилоте\n"
        f"👉 @MEXAHI3MBOT"
    )
    return await post(pool, bot, text)


# Ротирующие рекламные посты — создают FOMO через демонстрацию возможностей
_PROMO_POSTS = [
    (
        "🚀 <b>Что умеет Infragram прямо сейчас</b>\n\n"
        "За 5 минут ты можешь:\n"
        "• Опубликовать пост во все свои каналы одновременно\n"
        "• Запустить DM-кампанию на тысячи пользователей\n"
        "• Спарсить аудиторию из любого канала конкурента\n"
        "• Настроить авто-расписание публикаций на неделю вперёд\n\n"
        "Всё это без открытия каждого бота и канала вручную.\n\n"
        "💎 <b>Подписка — $29/мес</b>\n"
        "👉 @MEXAHI3MBOT"
    ),
    (
        "📩 <b>DM-кампании в Telegram — самый высокий CTR</b>\n\n"
        "Почему личные сообщения работают лучше постов:\n"
        "• Открываемость 80%+ vs 5-15% в каналах\n"
        "• Персонализация с именем, сегментом, историей\n"
        "• Printax ({Привет|Здравствуйте|Добрый день})\n"
        "• Ротация аккаунтов — безопасность без флуд-банов\n"
        "• Аналитика: доставлено / прочитано / ответили\n\n"
        "Infragram автоматизирует весь процесс.\n\n"
        "💎 <b>Попробуй — $29/мес</b>\n"
        "👉 @MEXAHI3MBOT"
    ),
    (
        "📢 <b>Фабрика каналов в Telegram</b>\n\n"
        "Хочешь 10, 50, 100 каналов?\n"
        "Infragram создаёт, настраивает и ведёт их массово:\n\n"
        "⚡ Создание каналов через сеть аккаунтов\n"
        "✏️ Bulk-редактирование: названия, описания, юзернеймы\n"
        "📅 Авто-расписание публикаций в каждый канал\n"
        "📊 Централизованная аналитика по всей сети\n"
        "🤖 @MEXAHI3MBOT как co-admin — контроль инфраструктуры\n\n"
        "💎 <b>Запусти свою сеть — $29/мес</b>\n"
        "👉 @MEXAHI3MBOT"
    ),
    (
        "🧠 <b>AI-ассистент для Telegram-маркетинга</b>\n\n"
        "Claude AI прямо внутри Infragram:\n\n"
        "📝 Анализ контента канала — что работает, что нет\n"
        "✍️ Генерация постов по теме и стилю\n"
        "📊 Разбор аудитории и сегментов\n"
        "💡 Идеи для DM-кампаний и воронок\n"
        "🔍 Анализ конкурентов по @username\n\n"
        "Безлимитные запросы — без отдельной подписки на AI.\n\n"
        "💎 <b>Включено в подписку — $29/мес</b>\n"
        "👉 @MEXAHI3MBOT"
    ),
    (
        "🌐 <b>Global Presence — массовое присутствие в Telegram</b>\n\n"
        "Твои аккаунты подписываются, вступают, реагируют — автоматически.\n\n"
        "Зачем это нужно:\n"
        "• Органический рост без покупки рекламы\n"
        "• Репосты и реакции поднимают охват постов\n"
        "• Присутствие в нужных группах и каналах\n"
        "• Сеть аккаунтов работает 24/7 пока ты спишь\n\n"
        "Настройка: 5 минут. Результат: постоянный входящий трафик.\n\n"
        "💎 <b>Запусти — $29/мес</b>\n"
        "👉 @MEXAHI3MBOT"
    ),
    (
        "⚔️ <b>Strike — защита и мониторинг каналов</b>\n\n"
        "Автоматический инструмент для активного продвижения:\n\n"
        "👁 Мониторинг упоминаний в реальном времени\n"
        "💬 Автоматические реакции и комментарии\n"
        "🤖 Выявление и нейтрализация ботов в аудитории\n"
        "📈 Буст охвата через синхронные действия сети\n\n"
        "Один запуск — работает на всю твою сеть каналов.\n\n"
        "💎 <b>Активируй Strike — $29/мес</b>\n"
        "👉 @MEXAHI3MBOT"
    ),
]


async def post_promo(pool: asyncpg.Pool, bot: Bot) -> bool:
    """Публикует ротирующий промо-пост в канал (FOMO через функции, не через числа)."""
    try:
        # Берём следующий по счётчику пост (детерминировано, не случайно)
        idx_raw = await pool.fetchval(
            "SELECT value FROM platform_settings WHERE key='bm_promo_idx'"
        )
        idx = (int(idx_raw) + 1) % len(_PROMO_POSTS) if idx_raw else 0
        await pool.execute(
            """INSERT INTO platform_settings (key, value, updated_at)
               VALUES ('bm_promo_idx', $1, NOW())
               ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
            str(idx),
        )
    except Exception:
        idx = random.randint(0, len(_PROMO_POSTS) - 1)

    return await post(pool, bot, _PROMO_POSTS[idx])


async def post_promo_offer(pool: asyncpg.Pool, bot: Bot) -> bool:
    """Публикует рекламный оффер для рекламодателей в канал."""
    text = (
        "📣 <b>Реклама через Infragram</b>\n\n"
        "<b>Аудитория:</b> владельцы Telegram-каналов, ботов и Telegram-маркетологи\n\n"
        "<b>Форматы:</b>\n"
        "📌 Пост в информационном канале\n"
        "🤖 Интеграция в бот Infragram\n"
        "📨 DM-рассылка по нашей пользовательской базе\n\n"
        "Аудитория активно вкладывает в развитие своих Telegram-активов — "
        "высокая конверсия в B2B и инструментальных нишах.\n\n"
        "💬 По вопросам сотрудничества: @MEXAHI3MBOT\n\n"
        "<i>Infragram — Telegram OS для роста инфраструктуры</i>"
    )
    return await post(pool, bot, text)
