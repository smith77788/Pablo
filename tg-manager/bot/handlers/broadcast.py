"""Broadcast composer and launcher."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import BroadcastCb, BotCb
from bot.keyboards import (
    broadcast_menu, broadcast_confirm, back_to_bot, broadcast_from_template,
    broadcast_history, broadcast_detail, broadcast_segment_menu,
)
from bot.states import Broadcast
from services import bot_api as _bot_api
from database import db
from services import broadcaster

router = Router()


@router.callback_query(BroadcastCb.filter(F.action == "menu"))
async def cb_bc_menu(callback: CallbackQuery, callback_data: BroadcastCb,
                      pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    count = await db.get_audience_count(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await callback.message.edit_text(
        f"📢 <b>Рассылка — {safe_label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Рассылка — это отправка одного сообщения сразу всем вашим подписчикам или их части. Как письмо, которое получают все сразу.\n\n"
        "💡 <b>Что можно делать:</b>\n"
        "• Отправить новость или акцию всем подписчикам\n"
        "• Выбрать только нужную часть аудитории (по тегам, активности)\n"
        "• Использовать готовый шаблон\n"
        "• Посмотреть историю прошлых рассылок\n\n"
        f"Активная аудитория: <b>{count}</b> чел.",
        parse_mode="HTML",
        reply_markup=broadcast_menu(callback_data.bot_id),
    )


@router.callback_query(BroadcastCb.filter(F.action == "compose"))
async def cb_compose(callback: CallbackQuery, callback_data: BroadcastCb,
                      state: FSMContext) -> None:
    await state.set_state(Broadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "✍️ Напишите текст рассылки или отправьте фото с подписью.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code>"
    )
    await callback.answer()


@router.message(Broadcast.waiting_message)
async def msg_broadcast_text(message: Message, state: FSMContext,
                              pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    segment_user_ids = data.get("segment_user_ids")
    if segment_user_ids:
        count = len(segment_user_ids)
    else:
        count = await db.get_audience_count(pool, data["bot_id"])

    if message.photo:
        photo_file_id = message.photo[-1].file_id
        text = message.caption or ""
    elif message.text:
        photo_file_id = None
        text = message.text
    else:
        await message.answer("❌ Отправьте текст или фото с подписью.")
        return

    await state.update_data(text=text, photo_file_id=photo_file_id)
    await state.set_state(Broadcast.confirming)

    if photo_file_id:
        preview_header = "📸 <b>Фото + подпись:</b>\n\n"
    else:
        preview_header = "📢 <b>Предпросмотр:</b>\n\n"

    segment_label = ""
    if data.get("segment_lang"):
        segment_label = f"🎯 Сегмент: <b>{data['segment_lang'].upper()}</b>\n"

    await message.answer(
        f"{preview_header}{text}\n\n"
        f"{segment_label}"
        f"Получателей: <b>{count}</b> чел.\nЗапустить?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(data["bot_id"]),
    )


@router.callback_query(BroadcastCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, callback_data: BroadcastCb,
                      state: FSMContext, pool: asyncpg.Pool,
                      http: aiohttp.ClientSession) -> None:

    data = await state.get_data()
    text = data.get("text", "")
    photo_file_id = data.get("photo_file_id")
    if not text and not photo_file_id:
        await callback.answer("Текст рассылки пуст.", show_alert=True)
        return
    await callback.answer()

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await state.clear()
        await callback.answer("Бот не найден.", show_alert=True)
        return

    segment_user_ids = data.get("segment_user_ids")
    if segment_user_ids:
        total = len(segment_user_ids)
    else:
        total = await db.get_audience_count(pool, row["bot_id"])
        segment_user_ids = None

    buttons = data.get("buttons")
    bc_id = await db.create_broadcast(pool, row["bot_id"], text, total, callback.from_user.id, photo_file_id)

    broadcaster.start(pool, http, bc_id, row["token"], row["bot_id"], text, photo_file_id,
                      segment_user_ids, buttons=buttons)

    await state.clear()
    await callback.message.edit_text(
        f"🚀 Рассылка #{bc_id} запущена!\n"
        f"Получателей: {total}\n\n"
        "Проверить статус можно в меню «📋 История».",
        reply_markup=back_to_bot(callback_data.bot_id),
    )


@router.callback_query(BroadcastCb.filter(F.action == "test"))
async def cb_test(callback: CallbackQuery, callback_data: BroadcastCb,
                  state: FSMContext, pool: asyncpg.Pool,
                  http: aiohttp.ClientSession) -> None:

    await callback.answer()
    data = await state.get_data()
    text = data.get("text", "")
    photo_file_id = data.get("photo_file_id")
    buttons = data.get("buttons")
    if not text and not photo_file_id:
        await callback.answer("Текст рассылки пуст.", show_alert=True)
        return

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    test_uid = callback.from_user.id
    if photo_file_id:
        ok, _ = await _bot_api.send_photo(http, row["token"], test_uid, photo_file_id, text, buttons=buttons)
    else:
        ok, _ = await _bot_api.send_message(http, row["token"], test_uid, text, buttons=buttons)

    if ok:
        await callback.answer("✅ Тест отправлен вам!", show_alert=True)
    else:
        await callback.answer("❌ Не удалось отправить тест. Убедитесь, что вы написали этому боту /start.", show_alert=True)


@router.callback_query(BroadcastCb.filter(F.action == "add_button"))
async def cb_add_button(callback: CallbackQuery, callback_data: BroadcastCb,
                        state: FSMContext) -> None:
    await state.set_state(Broadcast.waiting_button_text)
    await callback.message.edit_text(
        "🔗 <b>Добавить кнопку к рассылке</b>\n\nВведите текст кнопки:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Broadcast.waiting_button_text, F.text)
async def msg_button_text(message: Message, state: FSMContext) -> None:
    await state.update_data(pending_btn_text=message.text.strip())
    await state.set_state(Broadcast.waiting_button_url)
    await message.answer(
        f"🔗 Текст кнопки: <b>{message.text.strip()}</b>\n\nТеперь введите URL:",
        parse_mode="HTML",
    )


@router.message(Broadcast.waiting_button_url, F.text)
async def msg_button_url(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    url = message.text.strip()
    if not url.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ Неверный URL. Должен начинаться с http:// или https://")
        return

    data = await state.get_data()
    btn_text = data.get("pending_btn_text", "Открыть")
    buttons = data.get("buttons") or []
    buttons.append({"text": btn_text, "url": url})
    await state.update_data(buttons=buttons, pending_btn_text=None)
    await state.set_state(Broadcast.confirming)

    text = data.get("text", "")
    photo_file_id = data.get("photo_file_id")
    segment_label = f"🎯 Сегмент: <b>{data['segment_lang'].upper()}</b>\n" if data.get("segment_lang") else ""
    btn_list = "\n".join(f"  • {b['text']} → {b['url']}" for b in buttons)
    count = len(data.get("segment_user_ids") or []) or await db.get_audience_count(pool, data["bot_id"])

    preview_header = "📸 <b>Фото + подпись:</b>\n\n" if photo_file_id else "📢 <b>Предпросмотр:</b>\n\n"
    await message.answer(
        f"{preview_header}{text}\n\n"
        f"🔘 <b>Кнопки:</b>\n{btn_list}\n\n"
        f"{segment_label}"
        f"Получателей: <b>{count}</b> чел.\nЗапустить?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(data["bot_id"]),
    )


@router.callback_query(BroadcastCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    bot_id = data.get("bot_id", 0)
    await callback.message.edit_text(
        "❌ Рассылка отменена.",
        reply_markup=back_to_bot(bot_id) if bot_id else None,
    )
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "status"))
async def cb_status(callback: CallbackQuery, callback_data: BroadcastCb,
                    pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    history = await db.get_recent_broadcasts(pool, row["bot_id"], limit=10)
    if not history:
        await callback.answer("Рассылок пока не было.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await callback.message.edit_text(
        f"📋 <b>История рассылок — {safe_label}</b>\n\nНажмите на рассылку для деталей:",
        parse_mode="HTML",
        reply_markup=broadcast_history(callback_data.bot_id, history),
    )


@router.callback_query(BroadcastCb.filter(F.action == "detail"))
async def cb_detail(callback: CallbackQuery, callback_data: BroadcastCb,
                    pool: asyncpg.Pool) -> None:

    bc = await db.get_broadcast(pool, callback_data.broadcast_id)
    if not bc:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    await callback.answer()
    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}
    emoji = status_emoji.get(bc["status"], "❓")
    preview = bc["message_text"][:300] if bc["message_text"] else ""
    success_rate = round(bc["sent_count"] / bc["total_users"] * 100) if bc["total_users"] else 0
    finished = bc["finished_at"].strftime("%d.%m.%Y %H:%M") if bc.get("finished_at") else "—"
    progress_bar = ""
    if bc["status"] == "running" and bc["total_users"]:
        done = bc["sent_count"] + bc["failed_count"]
        pct = min(done * 100 // bc["total_users"], 100)
        filled = pct // 10
        progress_bar = f"\n{'█' * filled}{'░' * (10 - filled)} {pct}%\n"
    # Escape broadcast preview text to avoid HTML parse errors
    safe_preview = preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"📋 <b>Рассылка #{bc['id']}</b>\n\n"
        f"Статус: {emoji} {bc['status']}{progress_bar}\n"
        f"Создана: {bc['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
        f"Завершена: {finished}\n\n"
        f"Отправлено: <b>{bc['sent_count']}</b> / {bc['total_users']} ({success_rate}%)\n"
        f"Ошибок: {bc['failed_count']}\n\n"
        f"<b>Текст:</b>\n{safe_preview}"
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=broadcast_detail(callback_data.bot_id,
                                                                    bc["id"] if bc["status"] == "running" else None))


@router.callback_query(BroadcastCb.filter(F.action == "bc_summary"))
async def cb_bc_summary(callback: CallbackQuery, callback_data: BroadcastCb,
                         pool: asyncpg.Pool) -> None:
    """Show a concise summary of last 5 broadcasts with delivery stats."""
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    history = await db.get_broadcast_history(pool, callback_data.bot_id, limit=5)
    if not history:
        await callback.answer("Рассылок пока не было.", show_alert=True)
        return
    await callback.answer()

    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}
    lines = []
    total_sent = 0
    total_users = 0
    for bc in history:
        emoji = status_emoji.get(bc["status"], "❓")
        date_str = bc["created_at"].strftime("%d.%m.%Y %H:%M")
        sent = bc["sent_count"] or 0
        total = bc["total_users"] or 0
        delivery_pct = round(sent / total * 100) if total else 0
        bar_filled = delivery_pct // 10
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        lines.append(
            f"{emoji} <b>#{bc['id']}</b> {date_str}\n"
            f"   {bar} {delivery_pct}% ({sent}/{total})"
        )
        total_sent += sent
        total_users += total

    overall_pct = round(total_sent / total_users * 100) if total_users else 0
    summary_block = "\n\n".join(lines)

    await callback.message.edit_text(
        f"📈 <b>Сводка рассылок — {safe_label}</b>\n\n"
        f"{summary_block}\n\n"
        f"─────────────────\n"
        f"📊 Итого за 5 рассылок:\n"
        f"Отправлено: <b>{total_sent}</b> / {total_users} (<b>{overall_pct}%</b>)",
        parse_mode="HTML",
        reply_markup=broadcast_detail(callback_data.bot_id),
    )


@router.callback_query(BroadcastCb.filter(F.action == "from_template"))
async def cb_from_template(callback: CallbackQuery, callback_data: BroadcastCb,
                            pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    templates = await db.get_templates(pool, callback.from_user.id)
    if not templates:
        await callback.answer("У вас нет шаблонов. Создайте шаблон в разделе шаблонов.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "📋 <b>Выберите шаблон для рассылки:</b>",
        parse_mode="HTML",
        reply_markup=broadcast_from_template(callback_data.bot_id, templates),
    )


@router.callback_query(BroadcastCb.filter(F.action == "use_template"))
async def cb_use_template(callback: CallbackQuery, callback_data: BroadcastCb,
                           pool: asyncpg.Pool, state: FSMContext) -> None:

    # broadcast_id field repurposed here as template_id
    template = await db.get_template(pool, callback_data.broadcast_id, callback.from_user.id)
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    total = await db.get_audience_count(pool, callback_data.bot_id)
    if total == 0:
        await callback.answer("У бота нет аудитории для рассылки.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(Broadcast.confirming)
    await state.update_data(bot_id=callback_data.bot_id, text=template["text"])

    safe_name = template['name'].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_preview = template["text"][:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await callback.message.edit_text(
        f"📋 <b>Шаблон: {safe_name}</b>\n\n"
        f"Превью:\n{safe_preview}\n\n"
        f"👥 Получателей: <b>{total}</b>\n\n"
        "Отправить эту рассылку?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(callback_data.bot_id),
    )


@router.callback_query(BroadcastCb.filter(F.action == "segment"))
async def cb_segment(callback: CallbackQuery, callback_data: BroadcastCb,
                     pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    languages = await db.get_audience_languages(pool, callback_data.bot_id)
    if not languages:
        await callback.answer("Аудитория пуста.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await callback.message.edit_text(
        f"🎯 <b>Рассылка по сегменту — {safe_label}</b>\n\nВыберите язык аудитории:",
        parse_mode="HTML",
        reply_markup=broadcast_segment_menu(callback_data.bot_id, languages),
    )


@router.callback_query(BroadcastCb.filter(F.action == "segment_select"))
async def cb_segment_select(callback: CallbackQuery, callback_data: BroadcastCb,
                             state: FSMContext, pool: asyncpg.Pool) -> None:

    lang = callback_data.lang
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    if lang == "__new7__":
        user_ids = await db.get_audience_new_users(pool, callback_data.bot_id, 7)
        segment_label = "🆕 Новые за 7 дней"
    elif lang == "__new30__":
        user_ids = await db.get_audience_new_users(pool, callback_data.bot_id, 30)
        segment_label = "🆕 Новые за 30 дней"
    elif lang.startswith("__tag__"):
        tag = lang[7:]
        user_ids = await db.get_users_by_tag(pool, callback_data.bot_id, tag)
        segment_label = f"🏷 {tag}"
    else:
        user_ids = await db.get_audience_by_language(pool, callback_data.bot_id, lang)
        segment_label = f"🌍 {lang.upper()}"

    if not user_ids:
        await callback.answer("Нет пользователей в этом сегменте.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(Broadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id, segment_lang=lang, segment_user_ids=user_ids)
    await callback.message.edit_text(
        f"🎯 Сегмент: <b>{segment_label}</b> ({len(user_ids)} польз.)\n\n"
        "Напишите сообщение или отправьте фото для этого сегмента:",
        parse_mode="HTML",
    )
