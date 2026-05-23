"""Funnel (message chain) management handlers."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import FunnelCb, BotCb
from bot.keyboards import funnels_list, funnel_view, funnel_trigger_menu, back_to_bot, funnel_copy_target
from bot.states import CreateFunnel, FunnelBroadcast
from database import db
from services import broadcaster

router = Router()


async def _show_funnel_view(message: Message, pool: asyncpg.Pool,
                             bot_id: int, funnel_id: int) -> None:
    """Helper: fetch funnel + steps and edit/send view message."""
    funnels = await db.get_funnels(pool, bot_id)
    funnel = next((f for f in funnels if f["id"] == funnel_id), None)
    if not funnel:
        await message.answer("Цепочка не найдена.")
        return
    steps = await db.get_funnel_steps(pool, funnel_id)
    trigger = "/start" if funnel["trigger_type"] == "start" else f"🔑 {funnel['keyword']}"
    status = "✅ Активна" if funnel["is_active"] else "❌ Отключена"
    sub_ids = await db.get_funnel_subscriber_ids(pool, funnel_id)
    steps_text = ""
    for s in steps:
        delay_label = f"{s['delay_minutes']} мин" if s["delay_minutes"] > 0 else "сразу"
        steps_text += f"\n  <b>Шаг {s['step_order'] + 1}</b> [{delay_label}]: {s['message_text'][:60]}…"
    if not steps_text:
        steps_text = "\n  (нет шагов)"
    text = (
        f"🔗 <b>Цепочка: {funnel['name']}</b>\n\n"
        f"Триггер: {trigger}\n"
        f"Статус: {status}\n"
        f"Шагов: {len(steps)}\n"
        f"Подписчиков: <b>{len(sub_ids)}</b>\n\n"
        f"<b>Шаги:</b>{steps_text}"
    )
    await message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=funnel_view(bot_id, funnel_id, funnel["is_active"], len(steps)),
    )


# ── List ───────────────────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "list"))
async def cb_fn_list(callback: CallbackQuery, callback_data: FunnelCb,
                     pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🔗 <b>Цепочки сообщений — {label}</b>\n\n"
        f"Всего цепочек: <b>{len(funnels)}</b>\n\n"
        "Цепочка автоматически отправляет серию сообщений после триггера.",
        parse_mode="HTML",
        reply_markup=funnels_list(callback_data.bot_id, funnels),
    )
    await callback.answer()


# ── View ───────────────────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "view"))
async def cb_fn_view(callback: CallbackQuery, callback_data: FunnelCb,
                     pool: asyncpg.Pool) -> None:
    await _show_funnel_view(callback.message, pool, callback_data.bot_id, callback_data.funnel_id)
    await callback.answer()


# ── Create: ask name ───────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "create"))
async def cb_fn_create(callback: CallbackQuery, callback_data: FunnelCb,
                       state: FSMContext) -> None:
    await state.set_state(CreateFunnel.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "➕ <b>Новая цепочка</b>\n\nВведите название цепочки:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CreateFunnel.waiting_name)
async def msg_fn_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(funnel_name=message.text.strip())
    await state.set_state(CreateFunnel.waiting_trigger)
    await message.answer(
        f"📝 Название: <b>{message.text.strip()}</b>\n\nВыберите тип триггера:",
        parse_mode="HTML",
        reply_markup=funnel_trigger_menu(data["bot_id"]),
    )


# ── Trigger selection ──────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "trig_start"))
async def cb_fn_trig_start(callback: CallbackQuery, callback_data: FunnelCb,
                            state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    funnel_name = data.get("funnel_name", "Новая цепочка")
    bot_id = callback_data.bot_id or data.get("bot_id", 0)
    row = await db.create_funnel(pool, bot_id, funnel_name, "start")
    funnel_id = row["id"]
    await state.update_data(funnel_id=funnel_id, bot_id=bot_id, current_step=0)
    await state.set_state(CreateFunnel.waiting_step_text)
    await callback.message.edit_text(
        f"▶️ Триггер: <b>/start</b>\n\n"
        f"Цепочка создана! Теперь добавьте первый шаг.\n\n"
        "Введите текст сообщения для шага 1:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(FunnelCb.filter(F.action == "trig_keyword"))
async def cb_fn_trig_keyword(callback: CallbackQuery, callback_data: FunnelCb,
                              state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(bot_id=callback_data.bot_id or data.get("bot_id", 0))
    await state.set_state(CreateFunnel.waiting_keyword)
    await callback.message.edit_text(
        "🔑 Триггер: <b>Ключевое слово</b>\n\nВведите ключевое слово (регистр не важен):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CreateFunnel.waiting_keyword)
async def msg_fn_keyword(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    keyword = message.text.strip()
    funnel_name = data.get("funnel_name", "Новая цепочка")
    bot_id = data["bot_id"]
    row = await db.create_funnel(pool, bot_id, funnel_name, "keyword", keyword)
    funnel_id = row["id"]
    await state.update_data(funnel_id=funnel_id, current_step=0)
    await state.set_state(CreateFunnel.waiting_step_text)
    await message.answer(
        f"🔑 Ключевое слово: <code>{keyword}</code>\n\n"
        "Цепочка создана! Теперь добавьте первый шаг.\n\n"
        "Введите текст сообщения для шага 1:",
        parse_mode="HTML",
    )


# ── Add step ───────────────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "add_step"))
async def cb_fn_add_step(callback: CallbackQuery, callback_data: FunnelCb,
                         state: FSMContext) -> None:
    await state.set_state(CreateFunnel.waiting_step_text)
    await state.update_data(
        bot_id=callback_data.bot_id,
        funnel_id=callback_data.funnel_id,
        current_step=callback_data.step,
    )
    await callback.message.edit_text(
        f"➕ <b>Добавить шаг {callback_data.step + 1}</b>\n\nВведите текст сообщения:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CreateFunnel.waiting_step_text)
async def msg_fn_step_text(message: Message, state: FSMContext) -> None:
    await state.update_data(step_text=message.text)
    await state.set_state(CreateFunnel.waiting_step_delay)
    await message.answer(
        "⏱ Задержка в минутах перед отправкой этого шага (0 = сразу):",
        parse_mode="HTML",
    )


@router.message(CreateFunnel.waiting_step_delay)
async def msg_fn_step_delay(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    try:
        delay = int(message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое неотрицательное число (минуты):")
        return

    data = await state.get_data()
    await state.clear()
    funnel_id = data["funnel_id"]
    bot_id = data["bot_id"]
    step_order = data["current_step"]

    await db.add_funnel_step(pool, funnel_id, step_order, data["step_text"], delay)

    funnels = await db.get_funnels(pool, bot_id)
    funnel = next((f for f in funnels if f["id"] == funnel_id), None)
    steps = await db.get_funnel_steps(pool, funnel_id)

    trigger = "/start" if funnel and funnel["trigger_type"] == "start" else f"🔑 {funnel['keyword'] if funnel else ''}"
    status = "✅ Активна" if funnel and funnel["is_active"] else "❌ Отключена"
    steps_text = ""
    for s in steps:
        delay_label = f"{s['delay_minutes']} мин" if s["delay_minutes"] > 0 else "сразу"
        steps_text += f"\n  <b>Шаг {s['step_order'] + 1}</b> [{delay_label}]: {s['message_text'][:60]}…"
    if not steps_text:
        steps_text = "\n  (нет шагов)"

    await message.answer(
        f"✅ Шаг {step_order + 1} добавлен!\n\n"
        f"🔗 <b>Цепочка: {funnel['name'] if funnel else ''}</b>\n\n"
        f"Триггер: {trigger}\n"
        f"Статус: {status}\n"
        f"Шагов: {len(steps)}\n\n"
        f"<b>Шаги:</b>{steps_text}",
        parse_mode="HTML",
        reply_markup=funnel_view(bot_id, funnel_id, funnel["is_active"] if funnel else True, len(steps)),
    )


# ── Toggle ─────────────────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "toggle"))
async def cb_fn_toggle(callback: CallbackQuery, callback_data: FunnelCb,
                       pool: asyncpg.Pool) -> None:
    await db.toggle_funnel(pool, callback_data.funnel_id, callback_data.bot_id)
    await _show_funnel_view(callback.message, pool, callback_data.bot_id, callback_data.funnel_id)
    await callback.answer("✅ Статус изменён.")


# ── Delete ─────────────────────────────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "delete"))
async def cb_fn_delete(callback: CallbackQuery, callback_data: FunnelCb,
                       pool: asyncpg.Pool) -> None:
    await db.delete_funnel(pool, callback_data.funnel_id, callback_data.bot_id)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    await callback.message.edit_text(
        f"🔗 <b>Цепочки сообщений — {label}</b>\n\n"
        f"Всего цепочек: <b>{len(funnels)}</b>\n\n"
        "Цепочка автоматически отправляет серию сообщений после триггера.",
        parse_mode="HTML",
        reply_markup=funnels_list(callback_data.bot_id, funnels),
    )
    await callback.answer("🗑 Цепочка удалена.")


# ── Broadcast to funnel subscribers ───────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "broadcast"))
async def cb_fn_broadcast(callback: CallbackQuery, callback_data: FunnelCb,
                           pool: asyncpg.Pool, state: FSMContext) -> None:
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    funnel = next((f for f in funnels if f["id"] == callback_data.funnel_id), None)
    if not funnel:
        await callback.answer("Цепочка не найдена.", show_alert=True)
        return
    user_ids = await db.get_funnel_subscriber_ids(pool, callback_data.funnel_id)
    if not user_ids:
        await callback.answer("У цепочки нет подписчиков.", show_alert=True)
        return
    await state.set_state(FunnelBroadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id, funnel_id=callback_data.funnel_id,
                             funnel_name=funnel["name"], subscriber_ids=user_ids)
    await callback.message.edit_text(
        f"📢 <b>Рассылка подписчикам «{funnel['name']}»</b>\n\n"
        f"Подписчиков: <b>{len(user_ids)}</b>\n\n"
        "Введите текст сообщения (HTML поддерживается):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FunnelBroadcast.waiting_message)
async def msg_fn_broadcast(message: Message, state: FSMContext,
                            pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    await state.clear()

    row = await db.get_bot(pool, data["bot_id"], message.from_user.id)
    if not row:
        await message.answer("Бот не найден.")
        return

    user_ids = data["subscriber_ids"]
    bc_id = await db.create_broadcast(pool, data["bot_id"], message.text,
                                       len(user_ids), message.from_user.id, None)
    broadcaster.start(pool, http, bc_id, row["token"], data["bot_id"],
                      message.text, None, user_ids)

    await message.answer(
        f"🚀 Рассылка #{bc_id} запущена для <b>{len(user_ids)}</b> подписчиков цепочки «{data['funnel_name']}»!",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Copy funnels from another bot ─────────────────────────────────────────

@router.callback_query(FunnelCb.filter(F.action == "copy_from"))
async def cb_fn_copy_from(callback: CallbackQuery, callback_data: FunnelCb,
                           pool: asyncpg.Pool) -> None:
    bots = await db.get_bots(pool, callback.from_user.id)
    others = [b for b in bots if b["bot_id"] != callback_data.bot_id]
    if not others:
        await callback.answer("Нет других ботов для копирования.", show_alert=True)
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    await callback.message.edit_text(
        f"📋 <b>Скопировать цепочки в {label}</b>\n\nВыберите бот-источник:",
        parse_mode="HTML",
        reply_markup=funnel_copy_target(callback_data.bot_id, others),
    )
    await callback.answer()


@router.callback_query(FunnelCb.filter(F.action == "copy_confirm"))
async def cb_fn_copy_confirm(callback: CallbackQuery, callback_data: FunnelCb,
                              pool: asyncpg.Pool) -> None:
    src_bot = await db.get_bot(pool, callback_data.target_bot_id, callback.from_user.id)
    dst_bot = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not src_bot or not dst_bot:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    copied = await db.copy_funnels(pool, callback_data.target_bot_id, callback_data.bot_id)
    dst_label = f"@{dst_bot['username']}" if dst_bot["username"] else dst_bot["first_name"]
    src_label = f"@{src_bot['username']}" if src_bot["username"] else src_bot["first_name"]
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"🔗 <b>Цепочки сообщений — {dst_label}</b>\n\n"
        f"Всего цепочек: <b>{len(funnels)}</b>",
        parse_mode="HTML",
        reply_markup=funnels_list(callback_data.bot_id, funnels),
    )
    await callback.answer(f"✅ Скопировано {copied} цепочек из {src_label}!", show_alert=True)
