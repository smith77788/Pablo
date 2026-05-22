"""Auto-reply rules management for managed bots."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import asyncpg
from bot.callbacks import AutoReplyCb, BotCb
from bot.keyboards import auto_reply_menu, auto_reply_trigger_menu, auto_reply_view, back_to_bot
from bot.states import AddAutoReply
from database import db

router = Router()


@router.callback_query(AutoReplyCb.filter(F.action == "menu"))
async def cb_ar_menu(callback: CallbackQuery, callback_data: AutoReplyCb,
                     pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🤖 <b>Авто-ответы {label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in replies if r['is_active'])}</b> из {len(replies)}\n\n"
        "Бот автоматически отвечает на сообщения пользователей по заданным правилам.",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )
    await callback.answer()


@router.callback_query(AutoReplyCb.filter(F.action == "add"))
async def cb_ar_add(callback: CallbackQuery, callback_data: AutoReplyCb,
                    state: FSMContext) -> None:
    await state.set_state(AddAutoReply.choosing_trigger)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "➕ <b>Новое правило</b>\n\nВыберите тип триггера:",
        parse_mode="HTML",
        reply_markup=auto_reply_trigger_menu(callback_data.bot_id),
    )
    await callback.answer()


@router.callback_query(AutoReplyCb.filter(F.action == "trig_start"))
async def cb_trig_start(callback: CallbackQuery, callback_data: AutoReplyCb,
                        state: FSMContext) -> None:
    await state.update_data(trigger_type="start", keyword=None)
    await state.set_state(AddAutoReply.waiting_text)
    await callback.message.edit_text(
        "▶️ Триггер: <b>/start</b>\n\nВведите текст ответа (HTML-форматирование поддерживается):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AutoReplyCb.filter(F.action == "trig_keyword"))
async def cb_trig_keyword(callback: CallbackQuery, callback_data: AutoReplyCb,
                          state: FSMContext) -> None:
    await state.update_data(trigger_type="keyword")
    await state.set_state(AddAutoReply.waiting_keyword)
    await callback.message.edit_text(
        "🔑 Триггер: <b>Ключевое слово</b>\n\nВведите ключевое слово (регистр не важен):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AutoReplyCb.filter(F.action == "trig_any"))
async def cb_trig_any(callback: CallbackQuery, callback_data: AutoReplyCb,
                      state: FSMContext) -> None:
    await state.update_data(trigger_type="any", keyword=None)
    await state.set_state(AddAutoReply.waiting_text)
    await callback.message.edit_text(
        "💬 Триггер: <b>Любое сообщение</b>\n\nВведите текст ответа (HTML-форматирование поддерживается):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddAutoReply.waiting_keyword)
async def msg_ar_keyword(message: Message, state: FSMContext) -> None:
    await state.update_data(keyword=message.text.strip())
    await state.set_state(AddAutoReply.waiting_text)
    await message.answer(
        f"🔑 Ключевое слово: <code>{message.text.strip()}</code>\n\n"
        "Введите текст ответа (HTML-форматирование поддерживается):",
        parse_mode="HTML",
    )


@router.message(AddAutoReply.waiting_text)
async def msg_ar_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    await state.clear()
    await db.add_auto_reply(
        pool, data["bot_id"], data["trigger_type"],
        data.get("keyword"), message.text,
    )
    trigger_label = {
        "start": "/start",
        "keyword": f"🔑 {data.get('keyword')}",
        "any": "любое сообщение",
    }.get(data["trigger_type"])
    await message.answer(
        f"✅ Правило добавлено!\n\nТриггер: <b>{trigger_label}</b>",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "view"))
async def cb_ar_view(callback: CallbackQuery, callback_data: AutoReplyCb,
                     pool: asyncpg.Pool) -> None:
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    r = next((x for x in replies if x["id"] == callback_data.reply_id), None)
    if not r:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    trigger = {
        "start": "/start",
        "keyword": f"🔑 {r['keyword']}",
        "any": "💬 Любое сообщение",
    }.get(r["trigger_type"])
    status = "✅ Активно" if r["is_active"] else "❌ Отключено"
    await callback.message.edit_text(
        f"<b>Правило #{r['id']}</b>\n\n"
        f"Триггер: {trigger}\n"
        f"Статус: {status}\n\n"
        f"Ответ:\n{r['response_text']}",
        parse_mode="HTML",
        reply_markup=auto_reply_view(callback_data.bot_id, r["id"], r["is_active"]),
    )
    await callback.answer()


@router.callback_query(AutoReplyCb.filter(F.action == "toggle"))
async def cb_ar_toggle(callback: CallbackQuery, callback_data: AutoReplyCb,
                       pool: asyncpg.Pool) -> None:
    await db.toggle_auto_reply(pool, callback_data.reply_id, callback_data.bot_id)
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    await callback.message.edit_text(
        f"🤖 <b>Авто-ответы {label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in replies if r['is_active'])}</b> из {len(replies)}\n\n"
        "Бот автоматически отвечает на сообщения пользователей по заданным правилам.",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )
    await callback.answer("✅ Статус изменён.")


@router.callback_query(AutoReplyCb.filter(F.action == "delete"))
async def cb_ar_delete(callback: CallbackQuery, callback_data: AutoReplyCb,
                       pool: asyncpg.Pool) -> None:
    await db.delete_auto_reply(pool, callback_data.reply_id, callback_data.bot_id)
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    await callback.message.edit_text(
        f"🤖 <b>Авто-ответы {label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in replies if r['is_active'])}</b> из {len(replies)}\n\n"
        "Бот автоматически отвечает на сообщения пользователей по заданным правилам.",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )
    await callback.answer("🗑 Правило удалено.")
