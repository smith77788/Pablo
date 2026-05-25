from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import asyncpg
from bot.keyboards import main_menu
from bot.utils.subscription import get_plan, PLAN_LEVELS, PLAN_EMOJIS
from config import ADMIN_IDS
from database import db
from bot.handlers.admin import notify_new_platform_user

router = Router()


def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нет активного действия для отмены.")
        return
    await state.clear()
    await message.answer("❌ Действие отменено. Используйте /start для начала.")


@router.message(CommandStart())
async def cmd_start(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    # Check if blocked
    try:
        blocked = await pool.fetchval("SELECT 1 FROM blocked_users WHERE user_id=$1", uid)
        if blocked:
            await message.answer("⛔️ Ваш аккаунт заблокирован. Обратитесь в поддержку.")
            return
    except Exception:
        pass

    if not _is_admin(uid):
        await message.answer("⛔️ Доступ запрещён.")
        return

    # Track new platform users
    try:
        is_new = not await pool.fetchval(
            "SELECT 1 FROM platform_users WHERE user_id=$1", uid
        )
        await pool.execute(
            """INSERT INTO platform_users(user_id, username, first_name)
               VALUES($1,$2,$3)
               ON CONFLICT(user_id) DO UPDATE
               SET username=$2, first_name=$3, last_active=now()""",
            uid,
            message.from_user.username,
            message.from_user.first_name or "",
        )
        if is_new:
            await notify_new_platform_user(
                message.bot, pool, uid,
                message.from_user.username,
                message.from_user.first_name or "",
            )
    except Exception:
        pass

    bots = await db.get_bots(pool, uid)
    bot_count = len(bots)

    # Онбординг для новых пользователей без ботов
    if not bot_count:
        await message.answer(
            "👋 <b>Добро пожаловать в TG Manager!</b>\n\n"
            "Здесь вы управляете своими Telegram-ботами: рассылки, аудитория, автоответы, CRM и многое другое.\n\n"
            "🚀 <b>Начало работы — 3 шага:</b>\n"
            "1️⃣ Нажмите ➕ Добавить бота → вставьте токен от @BotFather\n"
            "2️⃣ Откройте бота из списка → изучите разделы\n"
            "3️⃣ Используйте Рассылка, Аудитория, Авто-ответы\n\n"
            "💡 <b>Подсказки:</b>\n"
            "• 🌐 Сеть &amp; операции — массовые действия сразу по всем ботам\n"
            "• 📱 Мои аккаунты — подключите личный Telegram-аккаунт\n"
            "• 🤖 AI-ассистент — задайте вопрос об управлении ботами\n\n"
            f"Ваш ID: <code>{uid}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    total_aud = sum(b["audience_count"] for b in bots if "audience_count" in b.keys())

    # Подсчёт активных рассылок по всем ботам пользователя
    active_broadcasts = 0
    try:
        bot_ids = [b["bot_id"] for b in bots]
        active_broadcasts = await pool.fetchval(
            "SELECT COUNT(*) FROM broadcasts WHERE bot_id = ANY($1::bigint[]) AND status IN ('pending', 'running')",
            bot_ids,
        ) or 0
    except Exception:
        active_broadcasts = 0

    stats_lines = [
        f"🤖 Ботов: <b>{bot_count}</b>",
        f"👥 Аудитория: <b>{total_aud}</b> чел.",
    ]
    if active_broadcasts:
        stats_lines.append(f"📢 Активных рассылок: <b>{active_broadcasts}</b>")
    summary = " · ".join(stats_lines[:2])
    extra = f"\n{stats_lines[2]}" if active_broadcasts else ""

    await message.answer(
        f"👋 <b>TG Manager</b>\n\n"
        f"{summary}{extra}\n\n"
        f"ID: <code>{uid}</code>\n\n"
        f"💡 Нажмите на бота из списка → откроется меню управления",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id

    if not _is_admin(uid):
        await message.answer("⛔️ Доступ запрещён.")
        return

    try:
        plan = await get_plan(pool, uid)
    except Exception:
        plan = "free"

    level = PLAN_LEVELS.get(plan, 0)
    emoji = PLAN_EMOJIS.get(plan, "🆓")

    text = (
        f"❓ <b>Справка TG Manager</b>\n\n"
        f"Ваш план: <b>{emoji} {plan.upper()}</b>\n\n"
        f"<b>📋 Команды:</b>\n"
        f"/start — главное меню\n"
        f"/help — эта справка\n"
        f"/subscription — управление подпиской и оплата\n"
        f"/ranking — трекер позиций в поиске Telegram\n"
        f"/accounts — мои Telegram-аккаунты\n"
        f"/ops — операции с аккаунтами (вступить, опубликовать, профиль)\n"
        f"/cancel — отменить текущее действие\n\n"
        f"<b>🤖 Управление ботами:</b>\n"
        f"Добавьте бота → выберите из списка → откроется меню с разделами:\n"
        f"• Аудитория — список пользователей бота\n"
        f"• Рассылка — отправить сообщение всем\n"
        f"• Команды — /start, /help и свои команды\n"
        f"• Авто-ответы — ответы на ключевые слова\n"
        f"• Шаблоны — готовые тексты для рассылок\n"
        f"• Inbox — живой чат с пользователями (STARTER+)\n"
        f"• Цепочки — воронки сообщений (STARTER+)\n"
        f"• CRM — теги и сегментация (STARTER+)\n"
        f"• A/B тесты — сравнение вариантов сообщений (PRO+)\n"
        f"• 📊 Позиции — позиция бота в поиске Telegram (STARTER+)\n\n"
        f"<b>🌐 Сеть &amp; операции:</b>\n"
        f"Управление всеми ботами одновременно — массовые правки имени, описания, команд, рассылка по всей сети, аналитика, кластеры.\n\n"
        f"<b>📡 Операции с аккаунтами:</b>\n"
        f"Через ваш личный Telegram-аккаунт: создать канал, вступить, публиковать посты, редактировать профиль.\n\n"
        f"💡 <b>Подсказка:</b> добавьте несколько ботов и подключите личный аккаунт, чтобы получить доступ ко всем функциям платформы."
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
