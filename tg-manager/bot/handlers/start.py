from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import asyncpg
from bot.keyboards import main_menu
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
    total_aud = sum(b["audience_count"] for b in bots if "audience_count" in b.keys())

    # Подсчёт активных рассылок по всем ботам пользователя
    active_broadcasts = 0
    if bot_count:
        try:
            bot_ids = [b["bot_id"] for b in bots]
            active_broadcasts = await pool.fetchval(
                "SELECT COUNT(*) FROM broadcasts WHERE bot_id = ANY($1::bigint[]) AND status IN ('pending', 'running')",
                bot_ids,
            ) or 0
        except Exception:
            active_broadcasts = 0

    if bot_count:
        stats_lines = [
            f"🤖 Ботов: <b>{bot_count}</b>",
            f"👥 Аудитория: <b>{total_aud}</b> чел.",
        ]
        if active_broadcasts:
            stats_lines.append(f"📢 Активных рассылок: <b>{active_broadcasts}</b>")
        summary = " · ".join(stats_lines[:2])
        extra = f"\n{stats_lines[2]}" if active_broadcasts else ""
    else:
        summary = "Добавьте первый бот по токену."
        extra = ""

    await message.answer(
        f"👋 <b>TG Manager</b>\n\n"
        f"{summary}{extra}\n\n"
        f"ID: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
