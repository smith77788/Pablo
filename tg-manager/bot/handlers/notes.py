"""Bot notes handler."""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
import asyncpg
from bot.callbacks import NoteCb
from bot.keyboards import back_to_bot
from database import db

router = Router()


class EditNote(StatesGroup):
    waiting_text = State()


@router.callback_query(NoteCb.filter(F.action == "edit"))
async def cb_note_edit(
    callback: CallbackQuery,
    callback_data: NoteCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    raw_note = row.get("note") or "(нет заметки)"
    current = raw_note.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await state.set_state(EditNote.waiting_text)
    await state.update_data(bot_id=callback_data.bot_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import BotCb
    _kb = InlineKeyboardBuilder()
    _kb.button(text="❌ Отмена", callback_data=BotCb(action="select", bot_id=callback_data.bot_id))
    _kb.adjust(1)
    await callback.message.edit_text(
        f"📝 <b>Заметка к боту</b>\n\nТекущая: <i>{current}</i>\n\n"
        "📌 <b>Что это?</b>\n"
        "Заметка — личная пометка для бота, видна только вам. Помогает не запутаться среди нескольких ботов.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "• Запишите назначение бота (например: «продажи», «тест»)\n"
        "• Отправьте «-» чтобы удалить заметку\n\n"
        "Отправьте новый текст заметки:",
        parse_mode="HTML",
        reply_markup=_kb.as_markup(),
    )


@router.message(EditNote.waiting_text, F.text)
async def msg_note_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.clear()
    note = "" if message.text.strip() == "-" else message.text.strip()
    await db.save_bot_note(pool, data["bot_id"], message.from_user.id, note)
    await message.answer(
        "✅ Заметка сохранена." if note else "🗑 Заметка удалена.",
        reply_markup=back_to_bot(data["bot_id"]),
    )
