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
        await callback.message.edit_text(
            locked_text("Сетевая рассылка v2", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
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
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка</b>\n\nЦель: {desc}\n\nНапишите текст сообщения (HTML поддерживается):",
        parse_mode="HTML",
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
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка</b>\n\nСегмент: {label}\n\nНапишите текст сообщения:",
        parse_mode="HTML",
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
    else:
        target_desc = segment

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=NetBcCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=NetBcCb(action="cancel"))
    kb.adjust(2)

    await message.answer(
        f"📢 <b>Предпросмотр сетевой рассылки</b>\n\n"
        f"{message.text}\n\n"
        f"<b>Цель:</b> {target_desc}\n\nЗапустить?",
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
    data = await state.get_data()
    await state.clear()
    text = data.get("text", "")
    segment = data.get("segment", "all_each")
    lang = data.get("lang", "")

    if not text:
        await callback.answer("Текст не найден.", show_alert=True)
        return
    await callback.answer()

    bots = await db.get_bots(pool, callback.from_user.id)
    total_started = 0
    total_users = 0

    if segment == "all_each":
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
    kb.button(text="◀️ Сеть & операции", callback_data=NetworkCb(action="menu"))
    await callback.message.edit_text(
        f"🚀 <b>Сетевая рассылка запущена!</b>\n\n"
        f"Ботов задействовано: <b>{total_started}</b>\n"
        f"Получателей: <b>{total_users:,}</b>\n\n"
        "Прогресс — в истории рассылок каждого бота.",
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
