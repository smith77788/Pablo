"""Message templates: save, list, use for broadcasts, delete."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import asyncpg
from bot.callbacks import TemplateCb, BotCb, BroadcastCb
from bot.keyboards import templates_list, template_actions, broadcast_confirm, back_to_bot
from bot.states import AddTemplate, Broadcast
from database import db

router = Router()


@router.callback_query(TemplateCb.filter(F.action == "list"))
async def cb_templates_list(callback: CallbackQuery, callback_data: TemplateCb,
                             pool: asyncpg.Pool) -> None:
    bot_id = callback_data.bot_id
    templates = await db.get_templates(pool, callback.from_user.id)
    count = len(templates)
    header = (
        f"📝 <b>Шаблоны сообщений</b>\n\nВсего: {count}"
        if count else
        "📝 <b>Шаблоны сообщений</b>\n\nШаблонов ещё нет."
    )
    await callback.message.edit_text(
        header,
        parse_mode="HTML",
        reply_markup=templates_list(templates, bot_id),
    )
    await callback.answer()


@router.callback_query(TemplateCb.filter(F.action == "add"))
async def cb_template_add(callback: CallbackQuery, callback_data: TemplateCb,
                           state: FSMContext) -> None:
    await state.set_state(AddTemplate.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "📝 <b>Новый шаблон — шаг 1/2</b>\n\n"
        "Введите название шаблона (например: <i>Акция</i>, <i>Приветствие</i>):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddTemplate.waiting_name)
async def msg_template_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip() if message.text else ""
    if not name:
        await message.answer("❌ Название не может быть пустым. Введите ещё раз:")
        return
    if len(name) > 64:
        await message.answer("❌ Название слишком длинное (макс. 64 символа). Введите ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AddTemplate.waiting_text)
    await message.answer(
        "📝 <b>Новый шаблон — шаг 2/2</b>\n\n"
        "Напишите текст сообщения.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
        "<code>&lt;a href=...&gt;ссылка&lt;/a&gt;</code>",
        parse_mode="HTML",
    )


@router.message(AddTemplate.waiting_text)
async def msg_template_text(message: Message, state: FSMContext,
                             pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    name = data["name"]
    text = message.text or message.caption or ""
    bot_id = data.get("bot_id", 0)
    await state.clear()

    if not text:
        await message.answer("❌ Текст шаблона не может быть пустым.")
        return

    saved = await db.save_template(pool, message.from_user.id, name, text)
    if saved:
        await message.answer(
            f"✅ Шаблон <b>«{name}»</b> сохранён!",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id) if bot_id else None,
        )
    else:
        await message.answer(
            f"⚠️ Шаблон с именем <b>«{name}»</b> уже существует. Выберите другое название.",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id) if bot_id else None,
        )


@router.callback_query(TemplateCb.filter(F.action == "view"))
async def cb_template_view(callback: CallbackQuery, callback_data: TemplateCb,
                            pool: asyncpg.Pool) -> None:
    tpl = await db.get_template(pool, callback_data.template_id, callback.from_user.id)
    if not tpl:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    preview = tpl["text"][:900] + ("…" if len(tpl["text"]) > 900 else "")
    await callback.message.edit_text(
        f"📝 <b>{tpl['name']}</b>\n\n{preview}",
        parse_mode="HTML",
        reply_markup=template_actions(callback_data.template_id, callback_data.bot_id),
    )
    await callback.answer()


@router.callback_query(TemplateCb.filter(F.action == "delete"))
async def cb_template_delete(callback: CallbackQuery, callback_data: TemplateCb,
                              pool: asyncpg.Pool) -> None:
    deleted = await db.delete_template(pool, callback_data.template_id, callback.from_user.id)
    if not deleted:
        await callback.answer("Не удалось удалить шаблон.", show_alert=True)
        return
    templates = await db.get_templates(pool, callback.from_user.id)
    bot_id = callback_data.bot_id
    count = len(templates)
    header = f"✅ Шаблон удалён. Осталось: {count}" if count else "✅ Шаблон удалён. Шаблонов больше нет."
    await callback.message.edit_text(
        f"📝 <b>Шаблоны</b>\n\n{header}",
        parse_mode="HTML",
        reply_markup=templates_list(templates, bot_id),
    )
    await callback.answer()


@router.callback_query(TemplateCb.filter(F.action == "use"))
async def cb_template_use(callback: CallbackQuery, callback_data: TemplateCb,
                           pool: asyncpg.Pool, state: FSMContext) -> None:
    tpl = await db.get_template(pool, callback_data.template_id, callback.from_user.id)
    if not tpl:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    bot_id = callback_data.bot_id
    count = await db.get_audience_count(pool, bot_id)
    preview = tpl["text"][:600] + ("…" if len(tpl["text"]) > 600 else "")

    await state.set_state(Broadcast.confirming)
    await state.update_data(bot_id=bot_id, text=tpl["text"])

    await callback.message.edit_text(
        f"📢 <b>Рассылка по шаблону «{tpl['name']}»</b>\n\n"
        f"{preview}\n\n"
        f"Получателей: <b>{count}</b> чел.\n\n"
        "Запустить рассылку?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(bot_id),
    )
    await callback.answer()
