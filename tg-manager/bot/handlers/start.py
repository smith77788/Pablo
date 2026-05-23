from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import asyncpg
from bot.keyboards import main_menu
from config import ADMIN_IDS

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

    await message.answer(
        f"👋 <b>TG Manager</b> — управление вашими Telegram-ботами\n\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Добавьте первый бот по токену или выберите из списка:",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
