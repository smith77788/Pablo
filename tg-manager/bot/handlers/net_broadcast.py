"""Network Broadcast v2 — send to each bot's own audience with segment filters."""

from __future__ import annotations
import asyncpg
import aiohttp
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import NetBcCb, NetworkCb
from bot.keyboards import net_broadcast_target_menu, net_broadcast_lang_menu
from bot.states import NetworkBroadcastV2
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from database import db
from services import broadcaster

router = Router()


@router.callback_query(NetBcCb.filter(F.action == "menu"))
async def cb_net_bc_menu(
    callback: CallbackQuery, callback_data: NetBcCb, pool: asyncpg.Pool
) -> None:
    await cb_net_bc_target(callback, callback_data, pool)


@router.callback_query(NetBcCb.filter(F.action == "choose_target"))
async def cb_net_bc_target(
    callback: CallbackQuery, callback_data: NetBcCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        from bot.callbacks import NetworkCb

        await callback.message.edit_text(
            locked_text("Сетевая рассылка v2", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "enterprise", back_callback=NetworkCb(action="menu")
            ),
        )
        return
    bots = await db.get_bots(pool, callback.from_user.id)
    total_aud = sum(b.get("audience_count", 0) for b in bots)
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка v2</b>\n\n"
        f"Ботов: <b>{len(bots)}</b>\n"
        f"Суммарная аудитория: <b>{total_aud:,}</b>\n\n"
        "Выберите цель рассылки:",
        parse_mode="HTML",
        reply_markup=net_broadcast_target_menu(),
    )


@router.callback_query(NetBcCb.filter(F.action == "choose_lang"))
async def cb_net_bc_lang(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "🌍 <b>Рассылка по языку — вся сеть</b>\n\nВыберите язык:",
        parse_mode="HTML",
        reply_markup=net_broadcast_lang_menu(),
    )


def _build_bot_pick_kb(bots: list, selected: list[int]) -> object:
    """Строит клавиатуру выбора ботов с чекбоксами."""
    kb = InlineKeyboardBuilder()
    for b in bots:
        bid = b["bot_id"]
        label = (
            f"@{b['username']}"
            if b.get("username")
            else b.get("first_name", f"Bot {bid}")
        )
        aud = b.get("audience_count", 0)
        check = "✅ " if bid in selected else "☐ "
        kb.button(
            text=f"{check}{label} ({aud:,} юз.)",
            callback_data=NetBcCb(action="toggle_bot", bot_id=bid),
        )
    has_selected = bool(selected)
    if has_selected:
        kb.button(
            text=f"▶️ Продолжить ({len(selected)} бот(ов))",
            callback_data=NetBcCb(action="bots_confirmed"),
        )
    kb.button(text="◀️ Назад", callback_data=NetBcCb(action="choose_target"))
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(NetBcCb.filter(F.action == "choose_bots"))
async def cb_net_bc_choose_bots(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NetBcCb(action="choose_target"))
        await callback.message.edit_text(
            "❌ <b>Нет ботов</b>\n\nДобавьте хотя бы одного бота для рассылки.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    await state.set_state(NetworkBroadcastV2.choosing_bots)
    await state.update_data(selected_bot_ids=[], segment="selected_bots")
    total = sum(b.get("audience_count", 0) for b in bots)
    await callback.message.edit_text(
        f"🤖 <b>Выбор ботов для рассылки</b>\n\n"
        f"Выберите ботов (можно несколько). Суммарная аудитория: <b>{total:,}</b>\n\n"
        "Нажмите на бота чтобы включить/выключить его в рассылку:",
        parse_mode="HTML",
        reply_markup=_build_bot_pick_kb(list(bots), []),
    )


@router.callback_query(NetBcCb.filter(F.action == "toggle_bot"))
async def cb_net_bc_toggle_bot(
    callback: CallbackQuery,
    callback_data: NetBcCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    _MAX_BOTS_SELECTION = 50
    await callback.answer()
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_bot_ids", []))
    bid = callback_data.bot_id
    if bid in selected:
        selected.remove(bid)
    else:
        if len(selected) >= _MAX_BOTS_SELECTION:
            await callback.answer(
                f"⚠️ Максимум {_MAX_BOTS_SELECTION} ботов для одной рассылки.",
                show_alert=True,
            )
            return
        selected.append(bid)
    await state.update_data(selected_bot_ids=selected)
    bots = await db.get_bots(pool, callback.from_user.id)
    await callback.message.edit_reply_markup(
        reply_markup=_build_bot_pick_kb(list(bots), selected)
    )


@router.callback_query(NetBcCb.filter(F.action == "bots_confirmed"))
async def cb_net_bc_bots_confirmed(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected: list[int] = data.get("selected_bot_ids", [])
    if not selected:
        await callback.answer("Выберите хотя бы одного бота.", show_alert=True)
        return
    bots = await db.get_bots(pool, callback.from_user.id)
    bots_map = {b["bot_id"]: b for b in bots}
    chosen = [bots_map[bid] for bid in selected if bid in bots_map]
    total_aud = sum(b.get("audience_count", 0) for b in chosen)
    names = ", ".join(
        f"@{b['username']}" if b.get("username") else b.get("first_name", "?")
        for b in chosen[:5]
    )
    if len(chosen) > 5:
        names += f" и ещё {len(chosen) - 5}"

    await state.update_data(segment="selected_bots")
    await state.set_state(NetworkBroadcastV2.waiting_message)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NetBcCb(action="choose_target"))
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка — выбранные боты</b>\n\n"
        f"Боты: <b>{names}</b>\n"
        f"Аудитория: <b>{total_aud:,}</b>\n\n"
        "Напишите текст сообщения (HTML поддерживается):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NetBcCb.filter(F.action == "choose_segment"))
async def cb_net_bc_segment(
    callback: CallbackQuery,
    callback_data: NetBcCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    segment = callback_data.segment
    bots = await db.get_bots(pool, callback.from_user.id)

    if segment == "all_each":
        total = sum(b.get("audience_count", 0) for b in bots)
        desc = f"каждый бот → своей аудитории (итого ≈{total:,} отправок)"
    elif segment == "unique":
        users = await db.get_unique_network_users(pool, callback.from_user.id)
        total = len(users)
        desc = f"уникальным пользователям сети ({total:,} юзеров)"
    else:
        desc = segment

    await state.set_state(NetworkBroadcastV2.waiting_message)
    await state.update_data(segment=segment, lang="")
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NetBcCb(action="choose_target"))
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка</b>\n\nЦель: {desc}\n\nНапишите текст сообщения (HTML поддерживается):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NetBcCb.filter(F.action == "type_message"))
async def cb_net_bc_type_msg(
    callback: CallbackQuery,
    callback_data: NetBcCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    segment = callback_data.segment
    lang = callback_data.lang or ""

    segment_labels = {
        "cold_all": "холодных (7–30 дн) по всей сети",
        "lost_all": "потерянных (30+ дн) по всей сети",
        "lang": f"язык: {lang} — по всей сети",
    }
    label = segment_labels.get(segment, segment)

    await state.set_state(NetworkBroadcastV2.waiting_message)
    await state.update_data(segment=segment, lang=lang)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NetBcCb(action="choose_target"))
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка</b>\n\nСегмент: {label}\n\nНапишите текст сообщения:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(NetworkBroadcastV2.waiting_message, F.text)
async def msg_net_bc_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.update_data(text=message.text)
    await state.set_state(NetworkBroadcastV2.confirming)

    segment = data.get("segment", "all_each")
    lang = data.get("lang", "")
    bots = await db.get_bots(pool, message.from_user.id)

    if segment == "all_each":
        total = sum(b.get("audience_count", 0) for b in bots)
        target_desc = f"каждый бот → своей аудитории ({total:,} отправок)"
    elif segment == "unique":
        users = await db.get_unique_network_users(pool, message.from_user.id)
        total = len(users)
        target_desc = f"{total:,} уникальных пользователей"
    elif segment == "cold_all":
        target_desc = "холодные (7–30 дн) по всей сети"
    elif segment == "lost_all":
        target_desc = "потерянные (30+ дн) по всей сети"
    elif segment == "lang":
        target_desc = f"язык: {lang} по всей сети"
    elif segment == "selected_bots":
        selected_ids: list[int] = data.get("selected_bot_ids", [])
        bots_map = {b["bot_id"]: b for b in bots}
        chosen = [bots_map[bid] for bid in selected_ids if bid in bots_map]
        total = sum(b.get("audience_count", 0) for b in chosen)
        names = ", ".join(
            f"@{b['username']}" if b.get("username") else b.get("first_name", "?")
            for b in chosen[:3]
        )
        if len(chosen) > 3:
            names += f" +{len(chosen) - 3}"
        target_desc = f"{len(chosen)} бот(ов): {names} ({total:,} юз.)"
    else:
        target_desc = segment

    # Ограничиваем превью длинных сообщений
    preview_text = message.text or ""
    if len(preview_text) > 500:
        preview_text = (
            preview_text[:500] + "...\n<i>[сообщение обрезано для предпросмотра]</i>"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=NetBcCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=NetBcCb(action="cancel"))
    kb.adjust(2)

    await message.answer(
        f"📢 <b>Предпросмотр сетевой рассылки</b>\n\n"
        f"{preview_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Цель:</b> {target_desc}\n\n"
        f"Запустить рассылку?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NetBcCb.filter(F.action == "confirm"))
async def cb_net_bc_confirm(
    callback: CallbackQuery,
    callback_data: NetBcCb,
    state: FSMContext,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    text = data.get("text", "")
    segment = data.get("segment", "all_each")
    lang = data.get("lang", "")

    if not text:
        await callback.message.edit_text(
            "⚠️ <b>Ошибка</b>\n\nТекст рассылки не найден. Попробуйте снова.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder()
            .button(text="◀️ Назад", callback_data=NetBcCb(action="choose_target"))
            .as_markup(),
        )
        return

    bots_all = await db.get_bots(pool, callback.from_user.id)
    if not bots_all:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text(
            "❌ <b>Нет ботов</b>\n\nДобавьте хотя бы одного бота для запуска сетевой рассылки.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Если выбраны конкретные боты — фильтруем
    if segment == "selected_bots":
        selected_ids: list[int] = data.get("selected_bot_ids", [])
        bots = [b for b in bots_all if b["bot_id"] in selected_ids]
        if not bots:
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu"))
            await callback.message.edit_text(
                "❌ <b>Боты не выбраны</b>\n\nПожалуйста, выберите хотя бы одного бота.",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
            return
    else:
        bots = bots_all

    total_started = 0
    total_users = 0

    if segment in ("all_each", "selected_bots"):
        for bot in bots:
            user_ids = await pool.fetch(
                "SELECT user_id FROM bot_users WHERE bot_id=$1", bot["bot_id"]
            )
            ids = [r["user_id"] for r in user_ids]
            if ids:
                bc_id = await db.create_broadcast(
                    pool, bot["bot_id"], text, len(ids), callback.from_user.id
                )
                if not bc_id:
                    continue
                broadcaster.start(
                    pool,
                    http,
                    bc_id,
                    bot["token"],
                    bot["bot_id"],
                    text,
                    None,
                    ids,
                    None,
                )
                total_started += 1
                total_users += len(ids)

    elif segment == "unique":
        users = await db.get_unique_network_users(pool, callback.from_user.id)
        from collections import defaultdict

        by_bot: dict = defaultdict(list)
        token_map: dict = {}
        for u in users:
            by_bot[u["bot_id"]].append(u["user_id"])
            token_map[u["bot_id"]] = u["token"]
        for bot_id, ids in by_bot.items():
            bc_id = await db.create_broadcast(
                pool, bot_id, text, len(ids), callback.from_user.id
            )
            if not bc_id:
                continue
            broadcaster.start(
                pool, http, bc_id, token_map[bot_id], bot_id, text, None, ids, None
            )
            total_started += 1
            total_users += len(ids)

    elif segment in ("cold_all", "lost_all"):
        days_from = 30 if segment == "lost_all" else 7
        days_to = None if segment == "lost_all" else 30
        for bot in bots:
            ids = await db.get_inactive_user_ids(
                pool, bot["bot_id"], days_from, days_to
            )
            if ids:
                bc_id = await db.create_broadcast(
                    pool, bot["bot_id"], text, len(ids), callback.from_user.id
                )
                if not bc_id:
                    continue
                broadcaster.start(
                    pool,
                    http,
                    bc_id,
                    bot["token"],
                    bot["bot_id"],
                    text,
                    None,
                    ids,
                    None,
                )
                total_started += 1
                total_users += len(ids)

    elif segment == "lang":
        for bot in bots:
            user_ids = await pool.fetch(
                "SELECT user_id FROM bot_users WHERE bot_id=$1 AND language_code=$2",
                bot["bot_id"],
                lang,
            )
            ids = [r["user_id"] for r in user_ids]
            if ids:
                bc_id = await db.create_broadcast(
                    pool, bot["bot_id"], text, len(ids), callback.from_user.id
                )
                if not bc_id:
                    continue
                broadcaster.start(
                    pool,
                    http,
                    bc_id,
                    bot["token"],
                    bot["bot_id"],
                    text,
                    None,
                    ids,
                    None,
                )
                total_started += 1
                total_users += len(ids)

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 История рассылок", callback_data=NetworkCb(action="menu"))
    kb.button(text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    if total_started == 0:
        kb_empty = InlineKeyboardBuilder()
        kb_empty.button(
            text="🔄 Выбрать другой сегмент",
            callback_data=NetBcCb(action="choose_target"),
        )
        kb_empty.button(
            text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu")
        )
        kb_empty.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Рассылка не запущена</b>\n\n"
            "Нет пользователей в выбранном сегменте.\n\n"
            "💡 <i>Попробуйте другой сегмент или подождите пока аудитория накопится в ботах.</i>",
            parse_mode="HTML",
            reply_markup=kb_empty.as_markup(),
        )
    else:
        segment_label = {
            "all_each": "Все боты → своей аудитории",
            "unique": "Уникальные пользователи сети",
            "cold_all": "Холодные (7–30 дн)",
            "lost_all": "Потерянные (30+ дн)",
            "lang": f"По языку: {lang}",
            "selected_bots": f"Выбранные боты ({total_started} шт.)",
        }.get(segment, segment)

        await callback.message.edit_text(
            f"🚀 <b>Сетевая рассылка запущена!</b>\n\n"
            f"📢 Сегмент: <b>{segment_label}</b>\n"
            f"🤖 Ботов задействовано: <b>{total_started}</b>\n"
            f"👥 Получателей: <b>{total_users:,}</b>\n\n"
            "⏳ Рассылка выполняется в фоне.\n"
            "Прогресс отображается в истории рассылок каждого бота.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


@router.callback_query(NetBcCb.filter(F.action == "cancel"))
async def cb_net_bc_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu"))
    await callback.message.edit_text(
        "❌ Рассылка отменена.", reply_markup=kb.as_markup()
    )
