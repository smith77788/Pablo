"""Presence Pack — link bot + channels + groups into a conversion funnel."""
from __future__ import annotations

import asyncio
import json
import logging
from html import escape

import aiohttp
import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import PackCb, BotAdminCb, BmCb, AutoReplyCb
from bot.states import PresencePackFSM
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from database import db
from services import presence_setup

log = logging.getLogger(__name__)
router = Router()


def _jlist(val) -> list:
    """Safe JSONB→list: asyncpg may return already-parsed list or a JSON string."""
    if isinstance(val, list):
        return val
    if val is None:
        return []
    try:
        return json.loads(val) or []
    except Exception:
        return []


async def _edit(cb: CallbackQuery, text: str, markup=None):
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


# ── Pack List ──────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "menu"))
async def cb_pack_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await _edit(callback, locked_text("Presence Packs", "starter"),
                    subscription_locked_markup("starter"))
        return

    packs = await db.get_presence_packs(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    for p in packs:
        ch_ids = _jlist(p["channel_ids"])
        gr_ids = _jlist(p["group_ids"])
        seed_icon = "🌱" if p["seed_posted"] else "⬜"
        admin_icon = "👑" if p["bot_promoted"] else ""
        status = f"{seed_icon}{admin_icon}"
        kb.button(
            text=f"{status} {p['name']} ({len(ch_ids)}📡 {len(gr_ids)}👥)",
            callback_data=PackCb(action="view", pack_id=p["id"]),
        )

    kb.button(text="➕ Создать пакет", callback_data=PackCb(action="create"))
    kb.button(text="◀️ Операции", callback_data=BmCb(action="operations"))
    kb.adjust(1)

    if packs:
        legend = "🌱 — посты опубликованы | 👑 — бот admin | ⬜ — ожидает"
        count_text = f"Пакетов: {len(packs)}\n<i>{legend}</i>"
    else:
        count_text = (
            "Пакетов пока нет.\n\n"
            "💡 <b>Что такое Presence Pack?</b>\n"
            "Это связка из бота + каналов + групп в единую воронку. "
            "Создайте пакет, назначьте бота администратором, опубликуйте "
            "посевные посты — и вся инфраструктура будет работать синхронно."
        )
    await _edit(
        callback,
        f"🗂 <b>Presence Packs</b>\n\n"
        f"{count_text}",
        markup=kb.as_markup(),
    )


# ── Create Pack — Step 1: Name ─────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "create"))
async def cb_pack_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PresencePackFSM.entering_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    await _edit(
        callback,
        "🗂 <b>Presence Pack</b> — Шаг 1/6\n\n"
        "Введите <b>название пакета</b> (например: «Магазин Москва», «Support Pack EU»):",
        markup=kb.as_markup(),
    )


@router.message(PresencePackFSM.entering_name)
async def fsm_pack_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    name = (message.text or "").strip()[:80]
    if not name:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
        await message.answer("❌ Название не может быть пустым. Введите название пакета:", reply_markup=kb.as_markup())
        return
    await state.update_data(pack_name=name)
    await state.set_state(PresencePackFSM.entering_description)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PackCb(action="skip_description"))
    kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    kb.adjust(1)
    await message.answer(
        "🗂 <b>Presence Pack</b> — Шаг 2/6\n\n"
        "Введите <b>описание пакета</b> (будет добавлено в посевные посты):\n"
        "Например: «Всё о криптовалютах и DeFi — новости, обзоры, сигналы»\n\n"
        "Или нажмите «⏭ Пропустить»",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(PackCb.filter(F.action == "skip_description"), PresencePackFSM.entering_description)
async def cb_pack_skip_description(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await _go_to_bot_step(callback, state, pool)


@router.message(PresencePackFSM.entering_description)
async def fsm_pack_description(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    desc = (message.text or "").strip()[:300]
    await state.update_data(pack_description=desc)
    await _go_to_bot_step(message, state, pool)


async def _go_to_bot_step(target: Message | CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.set_state(PresencePackFSM.selecting_bot)
    is_msg = isinstance(target, Message)
    uid = target.from_user.id

    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE LIMIT 20",
        uid,
    )
    kb = InlineKeyboardBuilder()
    for b in bots:
        label = f"@{b['username']}" if b.get("username") else (b.get("first_name") or f"id{b['bot_id']}")
        kb.button(text=f"🤖 {label}", callback_data=PackCb(action="pick_bot", pack_id=b["bot_id"]))
    kb.button(text="⏭ Без бота", callback_data=PackCb(action="pick_bot", pack_id=0))
    kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    kb.adjust(1)
    text = (
        "🗂 <b>Presence Pack</b> — Шаг 3/6\n\n"
        "Выберите <b>бот</b> для управления пользователями в пакете:"
    )
    if is_msg:
        await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Step 3: Bot ────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "pick_bot"), PresencePackFSM.selecting_bot)
async def cb_pack_pick_bot(
    callback: CallbackQuery, callback_data: PackCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bot_id = callback_data.pack_id
    bot_username = None
    if bot_id:
        bot_row = await pool.fetchrow(
            "SELECT username FROM managed_bots WHERE bot_id=$1", bot_id
        )
        bot_username = bot_row["username"] if bot_row else None
    await state.update_data(pack_bot_id=bot_id or None, pack_bot_username=bot_username)
    await state.set_state(PresencePackFSM.selecting_channels)
    await _render_channel_step(callback, state, pool)


async def _render_channel_step(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    selected: list[int] = sd.get("pack_channel_ids") or []

    channels = await pool.fetch(
        "SELECT id, title, username FROM managed_channels WHERE owner_id=$1 "
        "AND (type = 'channel' OR type IS NULL) ORDER BY title LIMIT 30",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    if not channels:
        kb.button(text="➡️ Пропустить (нет каналов)", callback_data=PackCb(action="channels_done"))
        kb.button(text="◀️ Назад к боту", callback_data=PackCb(action="back_to_bot"))
        kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
        kb.adjust(1)
        await _edit(
            callback,
            "🗂 <b>Presence Pack</b> — Шаг 4/6\n\n"
            "⚠️ У вас нет импортированных каналов.\n\n"
            "💡 Добавьте каналы через <b>/menu → 📱 Активы → 📡 Каналы → Импорт</b>, "
            "затем вернитесь сюда.\n\n"
            "Или продолжите без каналов:",
            markup=kb.as_markup(),
        )
        return
    for ch in channels:
        tick = "✅ " if ch["id"] in selected else ""
        label = (tick + (ch["title"] or ch.get("username") or f"id{ch['id']}")).strip()[:35]
        kb.button(text=label, callback_data=PackCb(action="toggle_ch", pack_id=ch["id"]))
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    nav.button(text=f"➡️ Далее ({len(selected)} выбрано)", callback_data=PackCb(action="channels_done"))
    nav.button(text="◀️ Назад к боту", callback_data=PackCb(action="back_to_bot"))
    nav.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    nav.adjust(1)
    kb.attach(nav)
    await _edit(
        callback,
        f"🗂 <b>Presence Pack</b> — Шаг 4/6\n\n"
        f"Выберите <b>каналы</b> для пакета (выбрано: {len(selected)}):\n"
        f"<i>Нажмите на канал чтобы добавить/убрать из пакета</i>",
        markup=kb.as_markup(),
    )


# ── Step 3: Channels ───────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "toggle_ch"), PresencePackFSM.selecting_channels)
async def cb_pack_toggle_ch(
    callback: CallbackQuery, callback_data: PackCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    sd = await state.get_data()
    selected: list[int] = list(sd.get("pack_channel_ids") or [])
    ch_id = callback_data.pack_id
    if ch_id in selected:
        selected.remove(ch_id)
    else:
        selected.append(ch_id)
    await state.update_data(pack_channel_ids=selected)
    await _render_channel_step(callback, state, pool)


@router.callback_query(PackCb.filter(F.action == "channels_done"), PresencePackFSM.selecting_channels)
async def cb_pack_channels_done(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await state.set_state(PresencePackFSM.selecting_groups)
    await _render_group_step(callback, state, pool)


async def _render_group_step(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    selected: list[int] = sd.get("pack_group_ids") or []

    groups = await pool.fetch(
        "SELECT id, title, username FROM managed_channels WHERE owner_id=$1 "
        "AND type IN ('megagroup', 'supergroup', 'group') ORDER BY title LIMIT 30",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    if not groups:
        kb.button(text="➡️ Продолжить без групп", callback_data=PackCb(action="groups_done"))
        kb.button(text="◀️ Назад к каналам", callback_data=PackCb(action="back_to_channels"))
        kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
        kb.adjust(1)
        await _edit(
            callback,
            "🗂 <b>Presence Pack</b> — Шаг 5/6\n\n"
            "ℹ️ У вас нет импортированных групп.\n\n"
            "💡 Добавьте группы через <b>/menu → 📱 Активы → 👥 Группы → Импорт</b>, "
            "или продолжите без групп:",
            markup=kb.as_markup(),
        )
        return
    for g in groups:
        tick = "✅ " if g["id"] in selected else ""
        label = (tick + (g["title"] or g.get("username") or "id" + str(g["id"]))).strip()[:35]
        kb.button(text=label, callback_data=PackCb(action="toggle_gr", pack_id=g["id"]))
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    nav.button(text=f"➡️ Далее ({len(selected)} выбрано)", callback_data=PackCb(action="groups_done"))
    nav.button(text="⏭ Без групп", callback_data=PackCb(action="groups_done"))
    nav.button(text="◀️ Назад к каналам", callback_data=PackCb(action="back_to_channels"))
    nav.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    nav.adjust(2, 2)
    kb.attach(nav)
    await _edit(
        callback,
        f"🗂 <b>Presence Pack</b> — Шаг 5/6\n\n"
        f"Выберите <b>группы/чаты</b> для пакета (выбрано: {len(selected)}):\n"
        f"<i>Нажмите на группу чтобы добавить/убрать из пакета</i>",
        markup=kb.as_markup(),
    )


# ── Step 4: Groups ─────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "toggle_gr"), PresencePackFSM.selecting_groups)
async def cb_pack_toggle_gr(
    callback: CallbackQuery, callback_data: PackCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    sd = await state.get_data()
    selected: list[int] = list(sd.get("pack_group_ids") or [])
    gr_id = callback_data.pack_id
    if gr_id in selected:
        selected.remove(gr_id)
    else:
        selected.append(gr_id)
    await state.update_data(pack_group_ids=selected)
    await _render_group_step(callback, state, pool)


@router.callback_query(PackCb.filter(F.action == "groups_done"), PresencePackFSM.selecting_groups)
async def cb_pack_groups_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PresencePackFSM.entering_target)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=PackCb(action="skip_target"))
    kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    kb.adjust(1)
    await _edit(
        callback,
        "🗂 <b>Presence Pack</b> — Шаг 6/6\n\n"
        "Введите <b>целевой ресурс</b> — ссылку или @username главного канала/бота/сайта.\n\n"
        "Формат — две строки:\n"
        "<code>@my_channel\nГлавный магазин</code>\n\n"
        "Или одна строка — только URL/username.",
        markup=kb.as_markup(),
    )


# ── Step 5: Target ─────────────────────────────────────────────────────────

@router.message(PresencePackFSM.entering_target)
async def fsm_pack_target(message: Message, state: FSMContext) -> None:
    lines = (message.text or "").strip().splitlines()
    target_url = lines[0].strip()[:200] if lines else ""
    target_label = lines[1].strip()[:80] if len(lines) > 1 else ""
    await state.update_data(pack_target_url=target_url, pack_target_label=target_label)
    await state.set_state(PresencePackFSM.previewing)
    await _render_preview_msg(message, state)


@router.callback_query(PackCb.filter(F.action == "skip_target"), PresencePackFSM.entering_target)
async def cb_pack_skip_target(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(pack_target_url=None, pack_target_label=None)
    await state.set_state(PresencePackFSM.previewing)
    await _render_preview_cb(callback, state)


async def _build_preview_text(sd: dict) -> str:
    name = sd.get("pack_name") or "—"
    description = sd.get("pack_description") or ""
    bot_username = sd.get("pack_bot_username") or "—"
    ch_ids = sd.get("pack_channel_ids") or []
    gr_ids = sd.get("pack_group_ids") or []
    target_url = sd.get("pack_target_url") or "—"
    target_label = sd.get("pack_target_label") or ""
    lines = [
        f"🗂 <b>Presence Pack — Предпросмотр</b>\n",
        f"<b>Название:</b> {escape(name)}",
    ]
    if description:
        lines.append(f"<b>Описание:</b> {escape(description[:100])}")
    lines += [
        f"<b>Бот:</b> @{escape(str(bot_username))}",
        f"<b>Каналов:</b> {len(ch_ids)}",
        f"<b>Групп:</b> {len(gr_ids)}",
        f"<b>Целевой ресурс:</b> {escape(target_label or target_url)}\n",
        f"После создания вы сможете:",
        f"• 🌱 Посеять начальные посты с взаимными ссылками",
        f"• 👑 Назначить бота администратором каналов",
        f"• 🔄 Синхронизировать настройки между зеркалами",
    ]
    return "\n".join(lines)


def _preview_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать pack", callback_data=PackCb(action="confirm_create"))
    kb.button(text="❌ Отмена", callback_data=PackCb(action="cancel_fsm"))
    kb.adjust(1)
    return kb


async def _render_preview_msg(message: Message, state: FSMContext) -> None:
    sd = await state.get_data()
    await message.answer(
        await _build_preview_text(sd), parse_mode="HTML",
        reply_markup=_preview_kb().as_markup(),
    )


async def _render_preview_cb(callback: CallbackQuery, state: FSMContext) -> None:
    sd = await state.get_data()
    await _edit(callback, await _build_preview_text(sd), markup=_preview_kb().as_markup())


@router.callback_query(PackCb.filter(F.action == "confirm_create"), PresencePackFSM.previewing)
async def cb_pack_confirm_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю пакет...")
    sd = await state.get_data()
    await state.clear()

    owner_id = callback.from_user.id
    pack_id = await db.create_presence_pack(
        pool, owner_id,
        name=sd.get("pack_name") or "Pack",
        description=sd.get("pack_description"),
        target_url=sd.get("pack_target_url"),
        target_label=sd.get("pack_target_label"),
        bot_id=sd.get("pack_bot_id"),
        bot_username=sd.get("pack_bot_username"),
    )
    if not pack_id:
        await callback.message.answer("❌ Ошибка создания пакета. Попробуйте ещё раз.")
        return
    ch_ids = sd.get("pack_channel_ids") or []
    gr_ids = sd.get("pack_group_ids") or []
    await db.update_presence_pack_channels(pool, pack_id, owner_id, ch_ids, gr_ids)

    name = sd.get("pack_name") or "Pack"
    bot_username = sd.get("pack_bot_username") or "—"
    kb = InlineKeyboardBuilder()
    kb.button(text="🌱 Посеять начальные посты", callback_data=PackCb(action="seed", pack_id=pack_id))
    kb.button(text="👑 Назначить бота admin", callback_data=PackCb(action="promote", pack_id=pack_id))
    kb.button(text="🔄 Синх. зеркала", callback_data=PackCb(action="mirror", pack_id=pack_id))
    kb.button(text="📋 Детали", callback_data=PackCb(action="view", pack_id=pack_id))
    kb.button(text="◀️ Все пакеты", callback_data=PackCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Presence Pack «{escape(name)}» создан!</b>\n\n"
        f"🤖 Бот: @{escape(str(bot_username))}\n"
        f"📡 Каналов: {len(ch_ids)} | 👥 Групп: {len(gr_ids)}\n\n"
        f"Что дальше:",
        markup=kb.as_markup(),
    )


# ── Pack View ──────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "view"))
async def cb_pack_view(
    callback: CallbackQuery, callback_data: PackCb, pool: asyncpg.Pool
) -> None:
    pack = await db.get_presence_pack(pool, callback_data.pack_id, callback.from_user.id)
    if not pack:
        await callback.answer("Пакет не найден", show_alert=True)
        return
    await callback.answer()

    ch_ids = _jlist(pack["channel_ids"])
    gr_ids = _jlist(pack["group_ids"])

    ch_rows = await pool.fetch(
        "SELECT title, username FROM managed_channels WHERE id = ANY($1::int[])", ch_ids
    ) if ch_ids else []
    gr_rows = await pool.fetch(
        "SELECT title, username FROM managed_channels WHERE id = ANY($1::int[])", gr_ids
    ) if gr_ids else []

    def _row_label(r) -> str:
        return r["title"] or r.get("username") or "—"

    ch_list = "\n".join(f"  • {_row_label(r)}" for r in ch_rows[:8]) or "  —"
    gr_list = "\n".join(f"  • {_row_label(r)}" for r in gr_rows[:5]) or "  —"

    bot_info = f"@{pack['bot_username']}" if pack.get("bot_username") else "не привязан"
    target = pack.get("target_url") or "—"
    target_label = pack.get("target_label") or ""
    seed_status = "✅ Опубликованы" if pack["seed_posted"] else "⬜ Не опубликованы"
    promoted_status = "✅ Назначен" if pack["bot_promoted"] else "⬜ Не назначен"

    # Build next-step guidance
    next_steps = []
    if not pack["bot_promoted"] and pack.get("bot_id"):
        next_steps.append("1️⃣ Назначьте бота администратором каналов (👑)")
    if not pack["seed_posted"]:
        next_steps.append("2️⃣ Опубликуйте начальные посты (🌱)")
    guidance = ("\n\n💡 <b>Следующие шаги:</b>\n" + "\n".join(next_steps)) if next_steps else "\n\n✅ <b>Пакет полностью настроен!</b>"

    pack_id = callback_data.pack_id
    kb = InlineKeyboardBuilder()
    if not pack["bot_promoted"] and pack.get("bot_id"):
        kb.button(text="👑 Назначить бота admin", callback_data=PackCb(action="promote", pack_id=pack_id))
    kb.button(text="🌱 Посеять начальные посты", callback_data=PackCb(action="seed", pack_id=pack_id))
    kb.button(text="🔄 Синх. зеркала", callback_data=PackCb(action="mirror", pack_id=pack_id))
    kb.button(text="🗑 Удалить", callback_data=PackCb(action="confirm_delete", pack_id=pack_id))
    kb.button(text="◀️ Все пакеты", callback_data=PackCb(action="menu"))
    kb.adjust(1)

    await _edit(
        callback,
        f"🗂 <b>{escape(pack['name'])}</b>\n"
        f"{'─' * 28}\n"
        f"🤖 Бот: {escape(bot_info)}\n"
        f"🎯 Цель: {escape(target_label or target)}\n"
        f"🌱 Посевные посты: {seed_status}\n"
        f"👑 Бот admin: {promoted_status}\n\n"
        f"📡 Каналы ({len(ch_ids)}):\n{ch_list}\n\n"
        f"👥 Группы ({len(gr_ids)}):\n{gr_list}"
        f"{guidance}",
        markup=kb.as_markup(),
    )


# ── Seed Posts ─────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "seed"))
async def cb_pack_seed(
    callback: CallbackQuery, callback_data: PackCb, pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    owner_id = callback.from_user.id
    pack = await db.get_presence_pack(pool, callback_data.pack_id, owner_id)
    if not pack:
        await callback.answer("Пакет не найден", show_alert=True)
        return

    ch_ids = _jlist(pack["channel_ids"])
    if not ch_ids:
        await callback.answer("Нет каналов в пакете", show_alert=True)
        return

    await callback.answer("⏳ Публикую начальные посты...")

    bot_token = None
    if pack.get("bot_id"):
        bot_row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            pack["bot_id"], owner_id,
        )
        if bot_row:
            bot_token = bot_row["token"]

    # Get group link for cross-linking
    gr_ids = _jlist(pack["group_ids"])
    group_link = None
    if gr_ids:
        gr_row = await pool.fetchrow(
            "SELECT username FROM managed_channels WHERE id = ANY($1::int[]) AND username IS NOT NULL LIMIT 1",
            gr_ids,
        )
        if gr_row:
            group_link = f"@{gr_row['username']}"

    channels = await pool.fetch(
        "SELECT title, username, channel_id, access_hash FROM managed_channels WHERE id = ANY($1::int[])",
        ch_ids,
    )

    success = 0
    fail = 0
    fail_details: list[str] = []
    for ch in channels:
        post_text = presence_setup.build_seed_post(
            channel_title=ch["title"] or ch.get("username") or pack["name"],
            bot_username=pack.get("bot_username"),
            group_link=group_link,
            target_url=pack.get("target_url"),
            target_label=pack.get("target_label"),
            pack_description=pack.get("description"),
        )
        chan_name = ch.get("title") or (f"@{ch['username']}" if ch.get("username") else f"id{ch['channel_id']}")
        posted = False
        if bot_token:
            # Bot API requires @username or -100{channel_id} format
            if ch.get("username"):
                chan_target = f"@{ch['username']}"
            else:
                chan_target = int(f"-100{ch['channel_id']}")
            posted = await presence_setup.seed_channel_post(http, bot_token, chan_target, post_text)
        if not posted:
            posted = await presence_setup.seed_channel_via_account(
                pool, owner_id, ch["channel_id"], ch.get("access_hash") or 0, post_text
            )
        if posted:
            success += 1
        else:
            fail += 1
            fail_details.append(chan_name)
        await asyncio.sleep(2)

    if success > 0:
        await db.mark_presence_pack_seeded(pool, callback_data.pack_id, owner_id)

    pack_id = callback_data.pack_id
    kb = InlineKeyboardBuilder()
    if fail > 0:
        kb.button(text="🔁 Повторить посев", callback_data=PackCb(action="seed", pack_id=pack_id))
    kb.button(text="📋 Детали пакета", callback_data=PackCb(action="view", pack_id=pack_id))
    kb.button(text="◀️ Все пакеты", callback_data=PackCb(action="menu"))
    kb.adjust(1)

    fail_hint = ""
    if fail > 0 and fail_details:
        names = ", ".join(fail_details[:3])
        extra = f" (+{len(fail_details)-3})" if len(fail_details) > 3 else ""
        fail_hint = (
            f"\n\n⚠️ Не удалось опубликовать в: <b>{escape(names)}{extra}</b>\n"
            f"💡 Причины: бот не является администратором канала, или аккаунт не участник. "
            f"Нажмите «👑 Назначить бота admin» и повторите посев."
        )
    elif fail > 0:
        fail_hint = "\n\n⚠️ Некоторые посты не опубликованы — убедитесь что бот является admin в каналах."

    if success == 0:
        result_text = (
            f"❌ <b>Посев не удался</b>\n\n"
            f"Ни один пост не был опубликован.\n"
            f"💡 Попробуйте:\n"
            f"1. Нажмите <b>👑 Назначить бота admin</b> — дайте боту права публикации\n"
            f"2. Убедитесь что аккаунты являются участниками каналов\n"
            f"3. Повторите посев{fail_hint}"
        )
    else:
        result_text = (
            f"🌱 <b>Посев завершён</b>\n\n"
            f"✅ Опубликовано: {success} | ❌ Ошибок: {fail}\n\n"
            f"Каналы содержат начальный пост с взаимными ссылками."
            f"{fail_hint}"
        )

    await _edit(callback, result_text, markup=kb.as_markup())


# ── Promote Bot as Admin ───────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "promote"))
async def cb_pack_promote(
    callback: CallbackQuery, callback_data: PackCb, pool: asyncpg.Pool,
) -> None:
    owner_id = callback.from_user.id
    pack = await db.get_presence_pack(pool, callback_data.pack_id, owner_id)
    if not pack or not pack.get("bot_id"):
        await callback.answer("Нет бота в пакете. Привяжите бот при создании.", show_alert=True)
        return

    ch_ids = _jlist(pack["channel_ids"])
    gr_ids = _jlist(pack["group_ids"])
    all_asset_ids = ch_ids + gr_ids
    if not all_asset_ids:
        await callback.answer("Нет каналов/групп в пакете", show_alert=True)
        return

    await callback.answer("⏳ Назначаю бота администратором...")

    channels = await pool.fetch(
        "SELECT channel_id, access_hash FROM managed_channels WHERE id = ANY($1::int[])",
        all_asset_ids,
    )

    bot_tg_id = pack["bot_id"]
    success = 0
    fail = 0
    for ch in channels:
        ok = await presence_setup.promote_bot_in_channel(
            pool, owner_id, ch["channel_id"], ch.get("access_hash") or 0, bot_tg_id
        )
        if ok:
            success += 1
        else:
            fail += 1
        await asyncio.sleep(2)

    await db.mark_presence_pack_promoted(pool, callback_data.pack_id, owner_id)

    pack_id = callback_data.pack_id
    kb = InlineKeyboardBuilder()
    kb.button(text="🌱 Посеять посты", callback_data=PackCb(action="seed", pack_id=pack_id))
    kb.button(text="📋 Детали", callback_data=PackCb(action="view", pack_id=pack_id))
    kb.button(text="◀️ Все пакеты", callback_data=PackCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"👑 <b>Бот назначен администратором</b>\n\n"
        f"✅ Успешно: {success} | ❌ Ошибок: {fail}\n\n"
        f"Права бота: публикация постов, приглашение пользователей.\n\n"
        f"💡 Теперь можно посеять посты через Bot API напрямую.",
        markup=kb.as_markup(),
    )


# ── Mirror Sync ────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "mirror"))
async def cb_pack_mirror(
    callback: CallbackQuery, callback_data: PackCb, pool: asyncpg.Pool,
) -> None:
    owner_id = callback.from_user.id
    pack = await db.get_presence_pack(pool, callback_data.pack_id, owner_id)
    if not pack or not pack.get("bot_id"):
        await callback.answer("Нет бота в пакете", show_alert=True)
        return

    await callback.answer("⏳ Синхронизирую зеркала...")

    synced, total = await presence_setup.mirror_sync_auto_replies(pool, pack["bot_id"], owner_id)

    pack_id = callback_data.pack_id
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали", callback_data=PackCb(action="view", pack_id=pack_id))
    kb.button(text="◀️ Все пакеты", callback_data=PackCb(action="menu"))
    kb.adjust(1)

    if total == 0:
        text = (
            "🔄 <b>Синхронизация зеркал</b>\n\n"
            "Зеркальные боты не найдены.\n\n"
            "💡 Чтобы синхронизировать настройки между ботами — "
            "объедините их в кластер: ⚙️ Мониторинг → Кластеры."
        )
    else:
        text = (
            f"🔄 <b>Синхронизация зеркал завершена</b>\n\n"
            f"Авто-ответы скопированы из главного бота в {synced}/{total} зеркал.\n\n"
            f"Все боты кластера теперь отвечают одинаково на команды."
        )
    await _edit(callback, text, markup=kb.as_markup())


# ── Delete Pack ────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "confirm_delete"))
async def cb_pack_confirm_delete(
    callback: CallbackQuery, callback_data: PackCb, pool: asyncpg.Pool,
) -> None:
    pack = await db.get_presence_pack(pool, callback_data.pack_id, callback.from_user.id)
    if not pack:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=PackCb(action="delete", pack_id=callback_data.pack_id))
    kb.button(text="◀️ Отмена", callback_data=PackCb(action="view", pack_id=callback_data.pack_id))
    kb.adjust(1)
    await _edit(
        callback,
        f"⚠️ Удалить пакет «{escape(pack['name'])}»?\n\n"
        "Каналы, боты и группы останутся нетронутыми — удаляется только конфигурация пакета.",
        markup=kb.as_markup(),
    )


@router.callback_query(PackCb.filter(F.action == "delete"))
async def cb_pack_delete(
    callback: CallbackQuery, callback_data: PackCb, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await db.delete_presence_pack(pool, callback_data.pack_id, callback.from_user.id)
    await cb_pack_menu(callback, pool)


# ── Cancel FSM ─────────────────────────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "cancel_fsm"))
async def cb_pack_cancel_fsm(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    await state.clear()
    await cb_pack_menu(callback, pool)


# ── Back navigation within wizard ─────────────────────────────────────────

@router.callback_query(PackCb.filter(F.action == "back_to_bot"))
async def cb_pack_back_to_bot(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Вернуться к шагу выбора бота из шага каналов."""
    await callback.answer()
    await _go_to_bot_step(callback, state, pool)


@router.callback_query(PackCb.filter(F.action == "back_to_channels"))
async def cb_pack_back_to_channels(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Вернуться к шагу каналов из шага групп."""
    await callback.answer()
    await state.set_state(PresencePackFSM.selecting_channels)
    await _render_channel_step(callback, state, pool)


# ── Bot Admin Panel ────────────────────────────────────────────────────────

@router.callback_query(BotAdminCb.filter(F.action == "panel"))
async def cb_bot_admin_panel(
    callback: CallbackQuery, callback_data: BotAdminCb, pool: asyncpg.Pool,
) -> None:
    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    bot_row = await pool.fetchrow(
        "SELECT username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        bot_id, owner_id,
    )
    if not bot_row:
        await callback.answer("Бот не найден", show_alert=True)
        return
    await callback.answer()

    user_count = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id
    ) or 0
    reply_count = await pool.fetchval(
        "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=TRUE", bot_id
    ) or 0
    funnel_count = await pool.fetchval(
        "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id
    ) or 0

    token = await db.get_bot_admin_token(pool, bot_id)
    bot_name = f"@{bot_row['username']}" if bot_row.get("username") else bot_row.get("first_name") or f"id{bot_id}"

    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Список авто-ответов", callback_data=BotAdminCb(action="list_replies", bot_id=bot_id))
    kb.button(text="🔑 Обновить токен доступа", callback_data=BotAdminCb(action="regen_token", bot_id=bot_id))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)
    await _edit(
        callback,
        f"🔧 <b>Admin панель: {escape(bot_name)}</b>\n\n"
        f"👥 Пользователей: {user_count}\n"
        f"💬 Авто-ответов: {reply_count}\n"
        f"🔄 Активных воронок: {funnel_count}\n\n"
        f"🔑 <b>Команда для входа в бота:</b>\n"
        f"<code>/admin {token or 'нет токена — нажмите «Обновить токен»'}</code>\n\n"
        f"<i>Введите эту команду в своём боте чтобы получить панель управления.</i>",
        markup=kb.as_markup(),
    )


@router.callback_query(BotAdminCb.filter(F.action == "regen_token"))
async def cb_bot_regen_token(
    callback: CallbackQuery, callback_data: BotAdminCb, pool: asyncpg.Pool,
) -> None:
    new_token = presence_setup.generate_admin_token()
    await db.upsert_bot_admin_session(pool, callback_data.bot_id, callback.from_user.id, new_token)
    # Show alert with new token, then refresh the panel.
    # cb_bot_admin_panel will call callback.answer() — skip it here to avoid double answer.
    await callback.answer(f"✅ Новый токен: /admin {new_token}", show_alert=True)
    # Refresh the panel directly without triggering another answer()
    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id
    bot_row = await pool.fetchrow(
        "SELECT username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        bot_id, owner_id,
    )
    if not bot_row:
        return
    user_count = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id
    ) or 0
    reply_count = await pool.fetchval(
        "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=TRUE", bot_id
    ) or 0
    funnel_count = await pool.fetchval(
        "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id
    ) or 0
    token = await db.get_bot_admin_token(pool, bot_id)
    bot_name = f"@{bot_row['username']}" if bot_row.get("username") else bot_row.get("first_name") or f"id{bot_id}"
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Список авто-ответов", callback_data=BotAdminCb(action="list_replies", bot_id=bot_id))
    kb.button(text="🔑 Обновить токен доступа", callback_data=BotAdminCb(action="regen_token", bot_id=bot_id))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)
    await _edit(
        callback,
        f"🔧 <b>Admin панель: {escape(bot_name)}</b>\n\n"
        f"👥 Пользователей: {user_count}\n"
        f"💬 Авто-ответов: {reply_count}\n"
        f"🔄 Активных воронок: {funnel_count}\n\n"
        f"🔑 <b>Команда для входа в бота:</b>\n"
        f"<code>/admin {token or 'нет токена — нажмите «Обновить токен»'}</code>\n\n"
        f"<i>Введите эту команду в своём боте чтобы получить панель управления.</i>",
        markup=kb.as_markup(),
    )


@router.callback_query(BotAdminCb.filter(F.action == "list_replies"))
async def cb_bot_list_replies(
    callback: CallbackQuery, callback_data: BotAdminCb, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.bot_id
    rules = await db.get_auto_replies(pool, bot_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=BotAdminCb(action="panel", bot_id=bot_id))
    kb.adjust(1)

    if not rules:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="➕ Добавить авто-ответ", callback_data=AutoReplyCb(action="add", bot_id=bot_id))
        empty_kb.button(text="◀️ Назад", callback_data=BotAdminCb(action="panel", bot_id=bot_id))
        empty_kb.adjust(1)
        await _edit(
            callback,
            "💬 <b>Авто-ответы</b>\n\nАвто-ответов пока нет.\n\n"
            "💡 Добавьте первый авто-ответ — бот будет автоматически реагировать на ключевые слова.",
            markup=empty_kb.as_markup(),
        )
        return

    lines = []
    for r in rules[:15]:
        status = "✅" if r["is_active"] else "⛔"
        kw = (r["keyword"] or "")[:35]
        lines.append(f"{status} <code>{escape(kw)}</code>")

    await _edit(
        callback,
        f"💬 <b>Авто-ответы бота</b> ({len(rules)}):\n\n" + "\n".join(lines) +
        "\n\n<i>Управление: ⚙️ Настройки → Авто-ответы</i>",
        markup=kb.as_markup(),
    )
