"""Funnel (message chain) management handlers."""

import html as _html
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import FunnelCb, BmCb
from bot.keyboards import (
    funnels_list,
    funnel_view,
    funnel_trigger_menu,
    back_to_bot,
    funnel_copy_target,
    subscription_locked_markup,
)
from bot.states import CreateFunnel, FunnelBroadcast
from bot.utils.subscription import require_plan, locked_text
from database import db
from services import broadcaster
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fn_cancel_kb(bot_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=FunnelCb(action="list", bot_id=bot_id))
    return kb.as_markup()


def _fn_back_cancel_kb(bot_id: int, back_action: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=FunnelCb(action=back_action, bot_id=bot_id))
    kb.button(text="❌ Отмена", callback_data=FunnelCb(action="list", bot_id=bot_id))
    kb.adjust(2)
    return kb.as_markup()


async def _owns_funnel(pool: asyncpg.Pool, funnel_id: int, user_id: int) -> bool:
    return bool(
        await pool.fetchval(
            """SELECT 1 FROM funnels f
           JOIN managed_bots b ON b.bot_id = f.bot_id
           WHERE f.id=$1 AND b.added_by=$2""",
            funnel_id,
            user_id,
        )
    )


async def _show_funnel_view(
    message: Message, pool: asyncpg.Pool, bot_id: int, funnel_id: int
) -> None:
    """Helper: fetch funnel + steps and edit/send view message."""
    funnels = await db.get_funnels(pool, bot_id)
    funnel = next((f for f in funnels if f["id"] == funnel_id), None)
    if not funnel:
        await message.answer("Цепочка не найдена.")
        return
    steps = await db.get_funnel_steps(pool, funnel_id)
    trigger = (
        "/start" if funnel["trigger_type"] == "start" else f"🔑 {funnel['keyword']}"
    )
    status = "✅ Активна" if funnel["is_active"] else "❌ Отключена"
    sub_ids = await db.get_funnel_subscriber_ids(pool, funnel_id)
    steps_text = ""
    for s in steps:
        delay_label = f"{s['delay_minutes']} мин" if s["delay_minutes"] > 0 else "сразу"
        raw_preview = s["message_text"][:60]
        preview = (
            raw_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        ellipsis = "…" if len(s["message_text"]) > 60 else ""
        steps_text += (
            f"\n  <b>Шаг {s['step_order'] + 1}</b> [{delay_label}]: {preview}{ellipsis}"
        )
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
async def cb_fn_list(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:

    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Цепочки сообщений", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="settings")
            ),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]

    # Fetch active subscriber counts for each funnel
    subscriber_counts: dict = {}
    for f in funnels:
        cnt = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM funnel_subscriptions WHERE funnel_id=$1 AND completed=false",
                f["id"],
            )
            or 0
        )
        subscriber_counts[f["id"]] = int(cnt)

    if not funnels:
        empty_text = (
            f"🔗 <b>Цепочки сообщений — {label}</b>\n\n"
            "📌 <b>Что это?</b>\n"
            "Цепочка — серия сообщений, которые бот автоматически отправляет пользователю с нужной задержкой.\n\n"
            "💡 У вас пока нет цепочек.\n"
            "Нажмите <b>➕ Создать</b>, чтобы добавить первую воронку!"
        )
    else:
        total_active_subs = sum(subscriber_counts.values())
        empty_text = (
            f"🔗 <b>Цепочки сообщений — {label}</b>\n\n"
            "📌 <b>Что это?</b>\n"
            "Цепочка (воронка) — это серия сообщений, которые бот отправляет пользователю автоматически одно за другим с нужной задержкой. Например: сразу после /start — приветствие, через 10 минут — первый урок, через 1 день — напоминание.\n\n"
            "💡 <b>Как использовать:</b>\n"
            "Создайте цепочку → добавьте шаги с текстом и задержкой → включите. Бот сам будет вести пользователей по шагам.\n\n"
            f"Цепочек создано: <b>{len(funnels)}</b> | Активных подписчиков: <b>{total_active_subs}</b>"
        )
    await callback.message.edit_text(
        empty_text,
        parse_mode="HTML",
        reply_markup=funnels_list(callback_data.bot_id, funnels, subscriber_counts),
    )


# ── View ───────────────────────────────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "view"))
async def cb_fn_view(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await _show_funnel_view(
        callback.message, pool, callback_data.bot_id, callback_data.funnel_id
    )


# ── Create: ask name ───────────────────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "create"))
async def cb_fn_create(
    callback: CallbackQuery, callback_data: FunnelCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(CreateFunnel.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "➕ <b>Новая цепочка</b>\n\nВведите название цепочки:",
        parse_mode="HTML",
        reply_markup=_fn_cancel_kb(callback_data.bot_id),
    )


@router.message(CreateFunnel.waiting_name, F.text)
async def msg_fn_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        data = await state.get_data()
        await message.answer(
            "⚠️ Название не может быть пустым. Введите снова:",
            reply_markup=_fn_cancel_kb(data.get("bot_id", 0)),
        )
        return
    if len(name) > 200:
        await message.answer(
            "⚠️ Слишком длинное название (макс. 200 символов). Введите снова:",
            reply_markup=_fn_cancel_kb((await state.get_data()).get("bot_id", 0)),
        )
        return
    data = await state.get_data()
    await state.update_data(funnel_name=name)
    await state.set_state(CreateFunnel.waiting_trigger)
    await message.answer(
        f"📝 Название: <b>{_html.escape(name)}</b>\n\nВыберите тип триггера:",
        parse_mode="HTML",
        reply_markup=funnel_trigger_menu(data["bot_id"]),
    )


# ── Trigger selection ──────────────────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "trig_start"))
async def cb_fn_trig_start(
    callback: CallbackQuery,
    callback_data: FunnelCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:

    await callback.answer()
    data = await state.get_data()
    funnel_name = data.get("funnel_name", "Новая цепочка")
    bot_id = callback_data.bot_id or data.get("bot_id", 0)
    row = await db.create_funnel(pool, bot_id, funnel_name, "start")
    funnel_id = row["id"]
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить шаг",
        callback_data=FunnelCb(
            action="add_step", bot_id=bot_id, funnel_id=funnel_id, step=0
        ),
    )
    kb.button(
        text="◀️ К списку",
        callback_data=FunnelCb(action="list", bot_id=bot_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "✅ <b>Воронка создана!</b>\n\n"
        f"🔗 <b>{_html.escape(funnel_name)}</b>\n"
        "▶️ Триггер: <b>/start</b>\n\n"
        "Добавьте первый шаг или вернитесь к списку воронок.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(FunnelCb.filter(F.action == "trig_keyword"))
async def cb_fn_trig_keyword(
    callback: CallbackQuery, callback_data: FunnelCb, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    bot_id = callback_data.bot_id or data.get("bot_id", 0)
    await state.update_data(bot_id=bot_id)
    await state.set_state(CreateFunnel.waiting_keyword)
    await callback.message.edit_text(
        "🔑 Триггер: <b>Ключевое слово</b>\n\nВведите ключевое слово (регистр не важен):",
        parse_mode="HTML",
        reply_markup=_fn_back_cancel_kb(bot_id, "create"),
    )


@router.message(CreateFunnel.waiting_keyword, F.text)
async def msg_fn_keyword(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    keyword = message.text.strip()
    if not keyword:
        await message.answer(
            "⚠️ Ключевое слово не может быть пустым. Введите снова:",
            reply_markup=_fn_cancel_kb(data.get("bot_id", 0)),
        )
        return
    if len(keyword) > 100:
        await message.answer(
            "⚠️ Слишком длинное ключевое слово (макс. 100 символов). Введите снова:",
            reply_markup=_fn_cancel_kb(data.get("bot_id", 0)),
        )
        return
    funnel_name = data.get("funnel_name", "Новая цепочка")
    bot_id = data["bot_id"]
    row = await db.create_funnel(pool, bot_id, funnel_name, "keyword", keyword)
    funnel_id = row["id"]
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить шаг",
        callback_data=FunnelCb(
            action="add_step", bot_id=bot_id, funnel_id=funnel_id, step=0
        ),
    )
    kb.button(
        text="◀️ К списку",
        callback_data=FunnelCb(action="list", bot_id=bot_id),
    )
    kb.adjust(1)
    await message.answer(
        "✅ <b>Воронка создана!</b>\n\n"
        f"🔗 <b>{_html.escape(funnel_name)}</b>\n"
        f"🔑 Ключевое слово: <code>{_html.escape(keyword)}</code>\n\n"
        "Добавьте первый шаг или вернитесь к списку воронок.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Add step ───────────────────────────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "add_step"))
async def cb_fn_add_step(
    callback: CallbackQuery, callback_data: FunnelCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(CreateFunnel.waiting_step_text)
    await state.update_data(
        bot_id=callback_data.bot_id,
        funnel_id=callback_data.funnel_id,
        current_step=callback_data.step,
    )
    await callback.message.edit_text(
        f"➕ <b>Добавить шаг {callback_data.step + 1}</b>\n\n"
        "Введите текст сообщения "
        "(поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code>):",
        parse_mode="HTML",
        reply_markup=_fn_cancel_kb(callback_data.bot_id),
    )


@router.message(CreateFunnel.waiting_step_text, F.text)
async def msg_fn_step_text(message: Message, state: FSMContext) -> None:
    text = message.text
    if not text or not text.strip():
        data = await state.get_data()
        await message.answer(
            "⚠️ Текст сообщения не может быть пустым. Введите снова:",
            reply_markup=_fn_cancel_kb(data.get("bot_id", 0)),
        )
        return
    await state.update_data(step_text=text)
    await state.set_state(CreateFunnel.waiting_step_delay)
    data = await state.get_data()
    await message.answer(
        "⏱ <b>Задержка в минутах</b> перед отправкой этого шага\n"
        "(0 = сразу после предыдущего шага).\n\n"
        "💡 <b>Примеры:</b>\n"
        "• <code>0</code> — сразу\n"
        "• <code>5</code> — через 5 минут\n"
        "• <code>60</code> — через 1 час\n"
        "• <code>1440</code> — через 1 день\n"
        "• <code>10080</code> — через 1 неделю",
        parse_mode="HTML",
        reply_markup=_fn_cancel_kb(data.get("bot_id", 0)),
    )


@router.message(CreateFunnel.waiting_step_delay, F.text)
async def msg_fn_step_delay(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        delay = int(message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        data_temp = await state.get_data()
        await message.answer(
            "Введите целое неотрицательное число (минуты):",
            reply_markup=_fn_cancel_kb(data_temp.get("bot_id", 0)),
        )
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

    trigger = (
        "/start"
        if funnel and funnel["trigger_type"] == "start"
        else f"🔑 {funnel['keyword'] if funnel else ''}"
    )
    status = "✅ Активна" if funnel and funnel["is_active"] else "❌ Отключена"
    steps_text = ""
    for s in steps:
        delay_label = f"{s['delay_minutes']} мин" if s["delay_minutes"] > 0 else "сразу"
        raw_preview = s["message_text"][:60]
        preview = (
            raw_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        ellipsis = "…" if len(s["message_text"]) > 60 else ""
        steps_text += (
            f"\n  <b>Шаг {s['step_order'] + 1}</b> [{delay_label}]: {preview}{ellipsis}"
        )
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
        reply_markup=funnel_view(
            bot_id, funnel_id, funnel["is_active"] if funnel else True, len(steps)
        ),
    )


# ── Toggle ─────────────────────────────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "toggle"))
async def cb_fn_toggle(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    await db.toggle_funnel(pool, callback_data.funnel_id, callback_data.bot_id)
    await _show_funnel_view(
        callback.message, pool, callback_data.bot_id, callback_data.funnel_id
    )
    await callback.answer("✅ Статус изменён.")


# ── Delete ─────────────────────────────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "delete"))
async def cb_fn_delete(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("🗑 Цепочка удалена.")
    await db.delete_funnel(pool, callback_data.funnel_id, callback_data.bot_id)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    label = (
        f"@{row['username']}"
        if row and row["username"]
        else (row["first_name"] if row else "")
    )
    await callback.message.edit_text(
        f"🔗 <b>Цепочки сообщений — {label}</b>\n\n"
        f"Всего цепочек: <b>{len(funnels)}</b>\n\n"
        "Цепочка автоматически отправляет серию сообщений после триггера.",
        parse_mode="HTML",
        reply_markup=funnels_list(callback_data.bot_id, funnels),
    )


# ── Broadcast to funnel subscribers ───────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "broadcast"))
async def cb_fn_broadcast(
    callback: CallbackQuery,
    callback_data: FunnelCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    funnels = await db.get_funnels(pool, callback_data.bot_id)
    funnel = next((f for f in funnels if f["id"] == callback_data.funnel_id), None)
    if not funnel:
        await callback.answer("Цепочка не найдена.", show_alert=True)
        return
    user_ids = await db.get_funnel_subscriber_ids(pool, callback_data.funnel_id)
    if not user_ids:
        await callback.answer("У цепочки нет подписчиков.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(FunnelBroadcast.waiting_message)
    await state.update_data(
        bot_id=callback_data.bot_id,
        funnel_id=callback_data.funnel_id,
        funnel_name=funnel["name"],
        subscriber_ids=user_ids,
    )
    await callback.message.edit_text(
        f"📢 <b>Рассылка подписчикам «{funnel['name']}»</b>\n\n"
        f"Подписчиков: <b>{len(user_ids)}</b>\n\n"
        "Введите текст сообщения (HTML поддерживается):",
        parse_mode="HTML",
        reply_markup=_fn_cancel_kb(callback_data.bot_id),
    )


@router.message(FunnelBroadcast.waiting_message, F.text)
async def msg_fn_broadcast(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    text = message.text.strip() if message.text else ""
    if not text:
        data = await state.get_data()
        await message.answer(
            "⚠️ Текст сообщения не может быть пустым. Введите снова:",
            reply_markup=_fn_cancel_kb(data.get("bot_id", 0)),
        )
        return
    data = await state.get_data()
    await state.clear()

    row = await db.get_bot(pool, data["bot_id"], message.from_user.id)
    if not row:
        await message.answer("Бот не найден.")
        return

    user_ids = data["subscriber_ids"]
    bc_id = await db.create_broadcast(
        pool, data["bot_id"], text, len(user_ids), message.from_user.id, None
    )
    broadcaster.start(
        pool, http, bc_id, row["token"], data["bot_id"], text, None, user_ids
    )

    await message.answer(
        f"🚀 Рассылка #{bc_id} запущена для <b>{len(user_ids)}</b> подписчиков цепочки «{data['funnel_name']}»!",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Copy funnels from another bot ─────────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "copy_from"))
async def cb_fn_copy_from(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:

    bots = await db.get_bots(pool, callback.from_user.id)
    others = [b for b in bots if b["bot_id"] != callback_data.bot_id]
    if not others:
        await callback.answer("Нет других ботов для копирования.", show_alert=True)
        return
    await callback.answer()
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = (
        f"@{row['username']}"
        if row and row["username"]
        else (row["first_name"] if row else "")
    )
    await callback.message.edit_text(
        f"📋 <b>Скопировать цепочки в {label}</b>\n\nВыберите бот-источник:",
        parse_mode="HTML",
        reply_markup=funnel_copy_target(callback_data.bot_id, others),
    )


@router.callback_query(FunnelCb.filter(F.action == "copy_confirm"))
async def cb_fn_copy_confirm(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:

    src_bot = await db.get_bot(pool, callback_data.target_bot_id, callback.from_user.id)
    dst_bot = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not src_bot or not dst_bot:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    copied = await db.copy_funnels(
        pool, callback_data.target_bot_id, callback_data.bot_id
    )
    dst_label = (
        f"@{dst_bot['username']}" if dst_bot["username"] else dst_bot["first_name"]
    )
    src_label = (
        f"@{src_bot['username']}" if src_bot["username"] else src_bot["first_name"]
    )
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"🔗 <b>Цепочки сообщений — {dst_label}</b>\n\n"
        f"Всего цепочек: <b>{len(funnels)}</b>",
        parse_mode="HTML",
        reply_markup=funnels_list(callback_data.bot_id, funnels),
    )
    await callback.answer(
        f"✅ Скопировано {copied} цепочек из {src_label}!", show_alert=True
    )


# ── Copy single funnel to another bot ─────────────────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "copy_single"))
async def cb_fn_copy_single(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Скопировать одну воронку в другой бот."""
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    funnel = next((f for f in funnels if f["id"] == callback_data.funnel_id), None)
    if not funnel:
        await callback.answer("Цепочка не найдена.", show_alert=True)
        return

    bots = await db.get_bots(pool, callback.from_user.id)
    others = [b for b in bots if b["bot_id"] != callback_data.bot_id]
    if not others:
        await callback.answer(
            "Нет других ботов. Добавьте второй бот чтобы копировать воронки.",
            show_alert=True,
        )
        return
    await callback.answer()

    kb = InlineKeyboardBuilder()
    for b in others[:8]:
        label = (
            f"@{b['username']}"
            if b.get("username")
            else (b.get("first_name") or f"id{b['bot_id']}")
        )
        kb.button(
            text=f"🤖 {label}",
            callback_data=FunnelCb(
                action="copy_single_confirm",
                bot_id=callback_data.bot_id,
                funnel_id=callback_data.funnel_id,
                target_bot_id=b["bot_id"],
            ),
        )
    kb.button(
        text="◀️ Назад",
        callback_data=FunnelCb(
            action="view",
            bot_id=callback_data.bot_id,
            funnel_id=callback_data.funnel_id,
        ),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        f"📋 <b>Копировать воронку «{funnel['name']}»</b>\n\nВыберите бот назначения:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(FunnelCb.filter(F.action == "copy_single_confirm"))
async def cb_fn_copy_single_confirm(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Выполнить копирование одной воронки."""
    src_funnel_id = callback_data.funnel_id
    dst_bot_id = callback_data.target_bot_id
    dst_bot = await db.get_bot(pool, dst_bot_id, callback.from_user.id)
    if not dst_bot:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    funnels = await db.get_funnels(pool, callback_data.bot_id)
    src_funnel = next((f for f in funnels if f["id"] == src_funnel_id), None)
    if not src_funnel:
        await callback.answer("Исходная воронка не найдена.", show_alert=True)
        return

    # Копируем шаги
    steps = await db.get_funnel_steps(pool, src_funnel_id)
    new_fn = await db.create_funnel(
        pool,
        dst_bot_id,
        src_funnel["name"] + " (копия)",
        src_funnel["trigger_type"],
        src_funnel.get("keyword"),
    )
    for s in steps:
        await db.add_funnel_step(
            pool, new_fn["id"], s["step_order"], s["message_text"], s["delay_minutes"]
        )

    dst_label = (
        f"@{dst_bot['username']}"
        if dst_bot.get("username")
        else (dst_bot.get("first_name") or str(dst_bot_id))
    )
    await callback.answer(f"✅ Воронка скопирована в {dst_label}!", show_alert=True)
    await _show_funnel_view(callback.message, pool, callback_data.bot_id, src_funnel_id)


# ── Steps management (reorder + delete + preview) ─────────────────────────


@router.callback_query(FunnelCb.filter(F.action == "steps_manage"))
async def cb_fn_steps_manage(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Показать список шагов с кнопками управления."""
    await callback.answer()
    steps = await db.get_funnel_steps(pool, callback_data.funnel_id)
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    funnel = next((f for f in funnels if f["id"] == callback_data.funnel_id), None)
    if not funnel:
        await callback.answer("Цепочка не найдена.", show_alert=True)
        return

    if not steps:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=FunnelCb(
                action="view",
                bot_id=callback_data.bot_id,
                funnel_id=callback_data.funnel_id,
            ),
        )
        await callback.message.edit_text(
            "📝 <b>Управление шагами</b>\n\nШагов пока нет. Добавьте шаги через кнопку «➕ Добавить шаг».",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines = []
    kb = InlineKeyboardBuilder()
    for s in steps:
        delay_label = f"{s['delay_minutes']} мин" if s["delay_minutes"] > 0 else "сразу"
        num = s["step_order"] + 1
        lines.append(f"<b>Шаг {num}</b> [{delay_label}]: {s['message_text'][:50]}")
        # Кнопки управления шагом
        if s["step_order"] > 0:
            kb.button(
                text=f"⬆️ {num}",
                callback_data=FunnelCb(
                    action="step_up",
                    bot_id=callback_data.bot_id,
                    funnel_id=callback_data.funnel_id,
                    step=s["step_order"],
                ),
            )
        if s["step_order"] < len(steps) - 1:
            kb.button(
                text=f"⬇️ {num}",
                callback_data=FunnelCb(
                    action="step_down",
                    bot_id=callback_data.bot_id,
                    funnel_id=callback_data.funnel_id,
                    step=s["step_order"],
                ),
            )
        kb.button(
            text=f"👁 {num}",
            callback_data=FunnelCb(
                action="step_preview",
                bot_id=callback_data.bot_id,
                funnel_id=callback_data.funnel_id,
                step=s["step_order"],
            ),
        )
        kb.button(
            text=f"🗑 {num}",
            callback_data=FunnelCb(
                action="step_delete",
                bot_id=callback_data.bot_id,
                funnel_id=callback_data.funnel_id,
                step=s["step_order"],
            ),
        )

    kb.button(
        text="◀️ Назад к воронке",
        callback_data=FunnelCb(
            action="view",
            bot_id=callback_data.bot_id,
            funnel_id=callback_data.funnel_id,
        ),
    )
    kb.adjust(1)

    steps_list = "\n".join(f"  {l}" for l in lines)
    await callback.message.edit_text(
        f"📝 <b>Управление шагами — «{funnel['name']}»</b>\n\n"
        f"{steps_list}\n\n"
        f"⬆️/⬇️ — переместить шаг | 👁 — предпросмотр | 🗑 — удалить",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(FunnelCb.filter(F.action == "step_preview"))
async def cb_fn_step_preview(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Показать полный текст шага."""
    steps = await db.get_funnel_steps(pool, callback_data.funnel_id)
    step = next((s for s in steps if s["step_order"] == callback_data.step), None)
    if not step:
        await callback.answer("Шаг не найден.", show_alert=True)
        return
    await callback.answer()
    delay_label = (
        f"{step['delay_minutes']} мин" if step["delay_minutes"] > 0 else "сразу"
    )
    num = step["step_order"] + 1
    raw = step["message_text"]
    safe = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад к шагам",
        callback_data=FunnelCb(
            action="steps_manage",
            bot_id=callback_data.bot_id,
            funnel_id=callback_data.funnel_id,
        ),
    )
    await callback.message.edit_text(
        f"👁 <b>Предпросмотр шага {num}</b> [задержка: {delay_label}]\n\n"
        f"<i>Содержимое сообщения:</i>\n\n"
        f"{safe}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(FunnelCb.filter(F.action == "step_delete"))
async def cb_fn_step_delete(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Удалить шаг из воронки и перенумеровать."""
    funnel_id = callback_data.funnel_id
    if not await _owns_funnel(pool, funnel_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer()
    step_to_delete = callback_data.step
    steps = await db.get_funnel_steps(pool, funnel_id)

    # Удаляем шаг и перенумеровываем
    await pool.execute(
        "DELETE FROM funnel_steps WHERE funnel_id=$1 AND step_order=$2",
        funnel_id,
        step_to_delete,
    )
    # Сдвинуть все шаги после удалённого на -1
    for s in steps:
        if s["step_order"] > step_to_delete:
            await pool.execute(
                "UPDATE funnel_steps SET step_order=$1 WHERE funnel_id=$2 AND step_order=$3",
                s["step_order"] - 1,
                funnel_id,
                s["step_order"],
            )

    await callback.answer(f"🗑 Шаг {step_to_delete + 1} удалён", show_alert=False)
    # Обновить экран управления шагами
    await cb_fn_steps_manage(callback, callback_data, pool)


async def _swap_step_content(
    pool: asyncpg.Pool, funnel_id: int, order_a: int, order_b: int
) -> None:
    """Обменять содержимое (message_text, delay_minutes) двух шагов по step_order.
    Так мы избегаем нарушения UNIQUE(funnel_id, step_order)."""
    row_a = await pool.fetchrow(
        "SELECT message_text, delay_minutes FROM funnel_steps WHERE funnel_id=$1 AND step_order=$2",
        funnel_id,
        order_a,
    )
    row_b = await pool.fetchrow(
        "SELECT message_text, delay_minutes FROM funnel_steps WHERE funnel_id=$1 AND step_order=$2",
        funnel_id,
        order_b,
    )
    if not row_a or not row_b:
        return
    await pool.execute(
        "UPDATE funnel_steps SET message_text=$1, delay_minutes=$2 WHERE funnel_id=$3 AND step_order=$4",
        row_b["message_text"],
        row_b["delay_minutes"],
        funnel_id,
        order_a,
    )
    await pool.execute(
        "UPDATE funnel_steps SET message_text=$1, delay_minutes=$2 WHERE funnel_id=$3 AND step_order=$4",
        row_a["message_text"],
        row_a["delay_minutes"],
        funnel_id,
        order_b,
    )


@router.callback_query(FunnelCb.filter(F.action == "step_up"))
async def cb_fn_step_up(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Переместить шаг вверх (обменять содержимое с предыдущим)."""
    if not await _owns_funnel(pool, callback_data.funnel_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    if callback_data.step == 0:
        await callback.answer("Шаг уже первый.", show_alert=True)
        return
    await callback.answer()
    await _swap_step_content(
        pool, callback_data.funnel_id, callback_data.step, callback_data.step - 1
    )
    await cb_fn_steps_manage(callback, callback_data, pool)


@router.callback_query(FunnelCb.filter(F.action == "step_down"))
async def cb_fn_step_down(
    callback: CallbackQuery, callback_data: FunnelCb, pool: asyncpg.Pool
) -> None:
    """Переместить шаг вниз (обменять содержимое со следующим)."""
    if not await _owns_funnel(pool, callback_data.funnel_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    steps = await db.get_funnel_steps(pool, callback_data.funnel_id)
    if callback_data.step >= len(steps) - 1:
        await callback.answer("Шаг уже последний.", show_alert=True)
        return
    await callback.answer()
    await _swap_step_content(
        pool, callback_data.funnel_id, callback_data.step, callback_data.step + 1
    )
    await cb_fn_steps_manage(callback, callback_data, pool)
