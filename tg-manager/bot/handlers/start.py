from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import asyncpg
from bot.keyboards import main_menu
from config import ADMIN_IDS
from database import db

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
    if not _is_admin(message.from_user.id):
        await message.answer("⛔️ Доступ запрещён.")
        return

    bots = await db.get_bots(pool, message.from_user.id)
    bot_count = len(bots)
    total_aud = sum(b["audience_count"] for b in bots if "audience_count" in b.keys())

    if bot_count:
        summary = f"Ботов: <b>{bot_count}</b> · Аудитория: <b>{total_aud}</b> чел."
    else:
        summary = "Добавьте первый бот по токену."

    await message.answer(
        f"👋 <b>TG Manager</b>\n\n"
        f"{summary}\n\n"
        f"ID: <code>{message.from_user.id}</code>",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
