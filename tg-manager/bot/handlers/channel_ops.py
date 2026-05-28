"""Channel & Account Operations handler.

Provides full Telegram account management via connected Telethon sessions:
  - Create channels/groups (single + bulk across all accounts)
  - Join / leave channels
  - Post content, send reactions
  - Edit channel settings (title, about, username, invite link, delete)
  - Manage members (view, invite, kick)
  - Edit account profile (name, bio, username)
  - Create bots via @BotFather automated dialog
  - Report content

Subscription gates:
  STARTER: join/leave, post, reactions, profile, report
  PRO:     create channel, member management, bulk, BotFather
"""
from __future__ import annotations

import asyncio
import html
import logging
import aiohttp
import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChanCb, ContactInvCb
from bot.states import (
    BulkCreateFSM, BulkDmFSM, BulkPostChansFSM, ContactInviteFSM, CreateBotFSM,
    CreateChannelFSM, EditChannelFSM, InviteUsersFSM, JoinChannelFSM, MyChannelsFSM,
    PostToChannelFSM, ReportFSM, SendReactionFSM, UpdateProfileFSM,
)
from bot.utils.subscription import require_plan
from bot.utils.op_helpers import _acc_label, _progress_bar, _progress_text
from services import session_simulator
from database import db

log = logging.getLogger(__name__)
router = Router()

_STARTER = "starter"
_PRO = "pro"

REPORT_REASONS = {
    "spam": "🚫 Спам",
    "violence": "⚠️ Насилие",
    "pornography": "🔞 Контент 18+",
    "childabuse": "🚨 Детский материал",
    "copyright": "©️ Нарушение авторских прав",
    "other": "📋 Другое",
}

REACTION_EMOJIS = ["👍", "❤️", "🔥", "🎉", "😮", "😢", "👎", "💯", "🤔", "🤩"]


def _backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff with ±20% jitter so operations don't look robotic."""
    import math, random
    raw = min(base ** attempt, cap)
    return raw * random.uniform(0.8, 1.2)


def _human_delay(min_s: float, max_s: float) -> float:
    """Return a random human-like delay between min_s and max_s seconds."""
    import random
    return random.uniform(min_s, max_s)


# ── Helpers ────────────────────────────────────────────────────────────────

async def _get_accounts(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT id, phone, first_name, username, is_active FROM tg_accounts "
        "WHERE owner_id=$1 ORDER BY added_at",
        owner_id,
    )


def _back_kb(acc_id: int = 0) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu", acc_id=acc_id))
    return kb


def _main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Создать канал",      callback_data=ChanCb(action="create_channel"))
    kb.button(text="👥 Создать группу",     callback_data=ChanCb(action="create_group"))
    kb.button(text="🔗 Вступить в канал",   callback_data=ChanCb(action="join"))
    kb.button(text="🚪 Выйти из канала",    callback_data=ChanCb(action="leave_pick"))
    kb.button(text="📤 Опубликовать пост",  callback_data=ChanCb(action="post_pick"))
    kb.button(text="✏️ Управление каналом", callback_data=ChanCb(action="manage_pick"))
    kb.button(text="👥 Участники",          callback_data=ChanCb(action="members_pick"))
    kb.button(text="🙋 Профиль аккаунта",   callback_data=ChanCb(action="profile_pick"))
    kb.button(text="👍 Реакция на пост",    callback_data=ChanCb(action="react_pick"))
    kb.button(text="🚨 Пожаловаться",       callback_data=ChanCb(action="report_pick"))
    kb.button(text="🤖 Создать бота",       callback_data=ChanCb(action="botfather_pick"))
    kb.button(text="⚡ Массовые операции",  callback_data=ChanCb(action="bulk_menu"))
    kb.button(text="📋 Мои каналы/чаты",   callback_data=ChanCb(action="my_chans"))
    kb.button(text="👥 Инвайт из контактов", callback_data=ChanCb(action="contact_invite"))
    kb.adjust(2, 2, 2, 2, 2, 2, 2)
    return kb


def _bulk_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Рассылка по username-списку", callback_data=ChanCb(action="bulk_dm"))
    kb.button(text="📢 Создать канал/группу",  callback_data=ChanCb(action="bulk_create"))
    kb.button(text="🔗 Вступить в канал",      callback_data=ChanCb(action="bulk_join"))
    kb.button(text="🚪 Выйти из канала",       callback_data=ChanCb(action="bulk_leave"))
    kb.button(text="📤 Пост в каналы",         callback_data=ChanCb(action="bulk_post_chans"))
    kb.button(text="📤 Опубликовать пост",     callback_data=ChanCb(action="bulk_post"))
    kb.button(text="✏️ Имя аккаунта",         callback_data=ChanCb(action="bulk_prof_name"))
    kb.button(text="📝 Bio аккаунта",          callback_data=ChanCb(action="bulk_prof_bio"))
    kb.button(text="🔤 Username аккаунта",     callback_data=ChanCb(action="bulk_prof_uname"))
    kb.button(text="◀️ Назад",                callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    return kb


def _make_title(base: str, mode: str, global_idx: int, acc_label: str) -> str:
    if mode == "num":
        return f"{base} {global_idx}"
    if mode == "acc":
        return f"{base} ({acc_label[:20]})"
    return base


# OP label map for display
_BULK_OP_LABELS = {
    "create":     "📢 Создать канал/группу",
    "botfather":  "🤖 Создать бота через @BotFather",
    "dm":         "✉️ Рассылка по username-списку",
    "join":       "🔗 Вступить в канал",
    "leave":      "🚪 Выйти из канала",
    "post":       "📤 Опубликовать пост",
    "prof_name":  "✏️ Изменить имя",
    "prof_bio":   "📝 Изменить bio",
    "prof_uname": "🔤 Изменить username",
}


def _bulk_select_kb(accounts: list, selected: set[int], op: str) -> InlineKeyboardBuilder:
    """Account selection keyboard with toggles."""
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc["id"] in selected else "☐"
        label = f"{icon} {_acc_label(acc)}"
        kb.button(text=label, callback_data=f"chan:bsel:{op}:{acc['id']}")
    n = len(selected)
    kb.button(text="✅ Выбрать все",  callback_data=f"chan:bsall:{op}")
    kb.button(text="☐ Снять все",    callback_data=f"chan:bsnone:{op}")
    if n > 0:
        kb.button(
            text=f"▶️ Продолжить с {n} аккаунт{'ом' if n==1 else 'ами'}",
            callback_data=f"chan:bsdone:{op}",
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_menu").pack())
    kb.adjust(1)
    return kb


def _account_picker_kb(accounts: list, action: str) -> InlineKeyboardBuilder:
    """Inline keyboard to pick one account for an action."""
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        label = ("✅ " if acc["is_active"] else "❌ ") + _acc_label(acc)
        kb.button(text=label, callback_data=ChanCb(action=action, acc_id=acc["id"]))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    return kb


async def _send_or_edit(msg_or_cb, text: str, kb, edit: bool = True) -> None:
    markup = kb.as_markup() if hasattr(kb, "as_markup") else kb
    if edit and hasattr(msg_or_cb, "message"):
        try:
            await msg_or_cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
        await msg_or_cb.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        target = msg_or_cb if hasattr(msg_or_cb, "answer") else msg_or_cb.message
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


# ── /ops entry point ───────────────────────────────────────────────────────

@router.message(Command("ops"))
async def cmd_ops(message: Message, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, message.from_user.id, _STARTER):
        await message.answer(
            "🔒 <b>Операции с аккаунтами — STARTER</b>\n\n"
            "Доступно с подпиской STARTER и выше.\n\nОформить: /subscription",
            parse_mode="HTML",
        )
        return
    accounts = await _get_accounts(pool, message.from_user.id)
    if not accounts:
        await message.answer(
            "⚠️ <b>Нет подключённых аккаунтов</b>\n\n"
            "Сначала подключите аккаунт Telegram: /accounts",
            parse_mode="HTML",
        )
        return
    count = len(accounts)
    active = sum(1 for a in accounts if a["is_active"])
    await message.answer(
        f"📡 <b>Операции с аккаунтами</b>\n\n"
        f"Подключено: <b>{count}</b> аккаунтов ({active} активных)\n\n"
        "Выберите действие:\n"
        "• Создать канал/группу — через ваш аккаунт\n"
        "• Вступить / Выйти — управление подписками\n"
        "• Опубликовать пост — от имени аккаунта\n"
        "• Управление каналом — название, описание, ссылка\n"
        "• Профиль — изменить имя, bio, username аккаунта\n"
        "• ⚡ Массовые операции — одно действие на нескольких аккаунтах сразу\n\n"
        "💡 Нет аккаунтов? Добавьте через 📱 Мои аккаунты",
        parse_mode="HTML",
        reply_markup=_main_menu_kb().as_markup(),
    )


# ── Main menu callback ─────────────────────────────────────────────────────

@router.callback_query(ChanCb.filter(F.action == "menu"))
async def cb_chan_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Операции с аккаунтами — STARTER</b>\n\n"
            "Для доступа нужна подписка STARTER или выше.\n\n"
            "Оформить: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    count = len(accounts)
    active = sum(1 for a in accounts if a["is_active"])
    await callback.message.edit_text(
        f"📡 <b>Операции с аккаунтами</b>\n\n"
        f"Подключено: <b>{count}</b> аккаунтов ({active} активных)\n\n"
        "Выберите действие:\n"
        "• Создать канал/группу — через ваш аккаунт\n"
        "• Вступить / Выйти — управление подписками\n"
        "• Опубликовать пост — от имени аккаунта\n"
        "• Управление каналом — название, описание, ссылка\n"
        "• Профиль — изменить имя, bio, username аккаунта\n"
        "• ⚡ Массовые операции — одно действие на нескольких аккаунтах сразу\n\n"
        "💡 Нет аккаунтов? Добавьте через 📱 Мои аккаунты",
        parse_mode="HTML",
        reply_markup=_main_menu_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# CREATE CHANNEL / GROUP (single account)
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action.in_({"create_channel", "create_group"})))
async def cb_create_pick_account(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Создание каналов/групп — PRO</b>\n\n"
            "Оформите подписку PRO: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    is_group = callback_data.action == "create_group"
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов. Проверьте подключение: /accounts",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    entity_type = "группу" if is_group else "канал"
    await state.update_data(is_group=is_group)
    if len(active) == 1:
        await state.update_data(acc_id=active[0]["id"], session_str=active[0]["session_str"] if "session_str" in active[0] else None)
        await _start_create_channel_fsm(callback.message, state, entity_type, edit=True)
        return
    kb = _account_picker_kb(active, "create_channel_acc" if not is_group else "create_group_acc")
    await callback.message.edit_text(
        f"📢 <b>Выберите аккаунт</b>\n\nС какого аккаунта создать {entity_type}?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action.in_({"create_channel_acc", "create_group_acc"})))
async def cb_create_account_chosen(
    callback: CallbackQuery,
    callback_data: ChanCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    is_group = callback_data.action == "create_group_acc"
    entity_type = "группу" if is_group else "канал"
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"], is_group=is_group)
    await _start_create_channel_fsm(callback.message, state, entity_type, edit=True)


async def _start_create_channel_fsm(msg, state: FSMContext, entity_type: str, edit: bool = False) -> None:
    await state.set_state(CreateChannelFSM.waiting_title)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    text = f"📝 <b>Название {entity_type}</b>\n\nВведите название (до 128 символов):"
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(CreateChannelFSM.waiting_title)
async def fsm_create_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer("⚠️ Название от 1 до 128 символов. Попробуйте ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(CreateChannelFSM.waiting_about)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ChanCb(action="skip_about"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "📄 <b>Описание</b>\n\nВведите описание (до 255 символов) или пропустите:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "skip_about"))
async def cb_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await _show_create_confirm(callback.message, state, edit=True)


@router.message(CreateChannelFSM.waiting_about)
async def fsm_create_about(message: Message, state: FSMContext) -> None:
    about = (message.text or "").strip()[:255]
    await state.update_data(about=about)
    await _show_create_confirm(message, state, edit=False)


async def _show_create_confirm(msg, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    title = html.escape(data.get("title", ""))
    about = html.escape(data.get("about", ""))
    is_group = data.get("is_group", False)
    entity_type = "Группа" if is_group else "Канал"
    text = (
        f"✅ <b>Подтвердите создание</b>\n\n"
        f"Тип: <b>{entity_type}</b>\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: <b>{about or '—'}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=ChanCb(action="do_create"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2)
    await state.set_state(CreateChannelFSM.confirming)
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanCb.filter(F.action == "do_create"))
async def cb_do_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю...")
    data = await state.get_data()
    await state.clear()
    acc_id = data.get("acc_id")
    if not acc_id:
        await callback.message.edit_text(
            "⚠️ Сессия истекла. Начните заново: /ops", parse_mode="HTML"
        )
        return
    acc = await db.get_account_for_telethon(pool, acc_id, callback.from_user.id)
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.", parse_mode="HTML")
        return

    from services import account_manager
    result = await account_manager.create_channel(
        acc["session_str"],
        title=data["title"],
        about=data.get("about", ""),
        megagroup=data.get("is_group", False),
        _acc=acc,
    )
    if "error" in result:
        err = html.escape(result["error"])
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания</b>\n\n<code>{err}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    title_s = html.escape(result["title"])
    channel_id = result["channel_id"]
    invite = result.get("invite_link", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Управлять", callback_data=ChanCb(action="manage_channel", acc_id=acc_id, channel_id=channel_id))
    kb.button(text="◀️ Меню", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>{result['type'].capitalize()} создан!</b>\n\n"
        f"Название: <b>{title_s}</b>\n"
        f"ID: <code>{channel_id}</code>\n"
        + (f"Ссылка: {html.escape(invite)}" if invite else ""),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK CREATE (all active accounts)
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "bulk_create"))
async def cb_bulk_create_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Массовое создание — PRO</b>\n\nОформите: /subscription",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", callback.from_user.id
    )
    selected = {a["id"] for a in accounts}
    await state.update_data(bulk_op="create", bulk_selected=list(selected))
    await _show_bulk_select(callback, pool, "create", selected)


@router.message(BulkCreateFSM.waiting_title)
async def fsm_bulk_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer("⚠️ Название от 1 до 128 символов:")
        return
    await state.update_data(title=title)
    await state.set_state(BulkCreateFSM.waiting_about)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ChanCb(action="bulk_skip_about"))
    kb.adjust(1)
    await message.answer(
        "📄 Описание (или пропустите):", parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ChanCb.filter(F.action == "bulk_skip_about"))
async def cb_bulk_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await _bulk_choose_type(callback.message, state, edit=True)


@router.message(BulkCreateFSM.waiting_about)
async def fsm_bulk_about(message: Message, state: FSMContext) -> None:
    await state.update_data(about=(message.text or "").strip()[:255])
    await _bulk_choose_type(message, state, edit=False)


async def _bulk_choose_type(msg, state: FSMContext, edit: bool) -> None:
    await state.set_state(BulkCreateFSM.choosing_type)
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Канал", callback_data=ChanCb(action="bulk_type_channel"))
    kb.button(text="👥 Группа", callback_data=ChanCb(action="bulk_type_group"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2, 1)
    text = "Тип создаваемого объекта:"
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanCb.filter(F.action.in_({"bulk_type_channel", "bulk_type_group"})))
async def cb_bulk_type_chosen(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    is_group = callback_data.action == "bulk_type_group"
    await state.update_data(is_group=is_group)
    await state.set_state(BulkCreateFSM.waiting_count)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await callback.message.edit_text(
        "📢 <b>Сколько каналов создать на каждом аккаунте?</b>\n\n"
        "Введите число от 1 до 10:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(BulkCreateFSM.waiting_count)
async def fsm_bulk_create_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        await message.answer("⚠️ Введите число от 1 до 10:")
        return
    count = int(raw)
    await state.update_data(channel_count=count)
    await state.set_state(BulkCreateFSM.choosing_name_mode)
    kb = InlineKeyboardBuilder()
    kb.button(text="☐ Без изменений",             callback_data=ChanCb(action="bulk_name_mode_none"))
    kb.button(text="🔢 Порядковый номер (1,2,3…)", callback_data=ChanCb(action="bulk_name_mode_num"))
    kb.button(text="👤 По аккаунту",               callback_data=ChanCb(action="bulk_name_mode_acc"))
    kb.button(text="❌ Отмена",                    callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)
    await message.answer(
        "📢 <b>Режим уникализации имён</b>\n\n"
        "Как назвать создаваемые объекты?\n\n"
        "• <b>Без изменений</b> — все получат одинаковое название\n"
        "• <b>Порядковый номер</b> — «Название 1», «Название 2»…\n"
        "• <b>По аккаунту</b> — «Название (Аккаунт1)», «Название (Аккаунт2)»",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action.in_({"bulk_name_mode_none", "bulk_name_mode_num", "bulk_name_mode_acc"})))
async def cb_bulk_name_mode(callback: CallbackQuery, callback_data: ChanCb, state: FSMContext) -> None:
    await callback.answer()
    mode_map = {
        "bulk_name_mode_none": "none",
        "bulk_name_mode_num": "num",
        "bulk_name_mode_acc": "acc",
    }
    mode = mode_map[callback_data.action]
    await state.update_data(name_mode=mode)
    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    n_acc = len(selected_ids)
    count = data.get("channel_count", 1)
    total = n_acc * count
    title_s = html.escape(data["title"])
    entity = "группа" if data.get("is_group") else "канал"
    mode_labels = {"none": "Без изменений", "num": "Порядковый номер", "acc": "По аккаунту"}
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Создать {total} объект(ов)", callback_data=ChanCb(action="do_bulk_create"))
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)
    await state.set_state(BulkCreateFSM.confirming)
    await callback.message.edit_text(
        f"🔁 <b>Подтверждение массового создания</b>\n\n"
        f"Тип: <b>{entity}</b>\n"
        f"Название: <b>{title_s}</b>\n"
        f"Режим имён: <b>{mode_labels[mode]}</b>\n"
        f"Аккаунтов: <b>{n_acc}</b> × <b>{count}</b> = итого <b>{total}</b>\n\n"
        "⚠️ Telegram может ограничить создание каналов с одного IP. Продолжить?",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "do_bulk_create"))
async def cb_do_bulk_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю массовое создание...")
    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    await state.clear()
    if selected_ids:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
            callback.from_user.id, selected_ids,
        )
    else:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.is_active=TRUE",
            callback.from_user.id,
        )
    from services import account_manager
    from database import db as _db
    channel_count = data.get("channel_count", 1)
    name_mode = data.get("name_mode", "none")
    results_ok, results_err = [], []
    total_ops = len(accounts) * channel_count
    done_ops = 0
    global_idx = 1
    attempt = 0
    progress_msg = await callback.message.edit_text(
        _progress_text("Создание каналов...", 0, total_ops, 0, 0),
        parse_mode="HTML",
    )
    # Round-robin: task i uses accounts[i % len(accounts)]
    active_accounts = list(accounts)
    tasks = []
    for i in range(total_ops):
        acc = active_accounts[i % len(active_accounts)] if active_accounts else None
        tasks.append((i, acc))

    for task_i, acc in tasks:
        if not acc:
            results_err.append(f"❌ Нет доступных аккаунтов")
            done_ops += 1
            global_idx += 1
            continue
        label = html.escape(acc["first_name"] or acc["phone"])
        title = _make_title(data["title"], name_mode, global_idx, acc["first_name"] or acc["phone"])
        # Account rotation on banned/flood_wait
        tried_accs = set()
        result = None
        for candidate in active_accounts:
            if candidate["id"] in tried_accs:
                continue
            tried_accs.add(candidate["id"])
            result = await account_manager.create_channel(
                candidate["session_str"],
                title=title,
                about=data.get("about", ""),
                megagroup=data.get("is_group", False),
                _acc=dict(candidate),
            )
            if result.get("banned"):
                await _db.deactivate_account(pool, candidate["id"], "banned detected in bulk op")
                active_accounts = [a for a in active_accounts if a["id"] != candidate["id"]]
                continue
            if result.get("flood_wait"):
                continue
            break
        if result is None:
            result = {"error": "нет доступных аккаунтов"}
        if "error" in result:
            results_err.append(f"❌ {html.escape(label)}: {html.escape(result['error'][:60])}")
        else:
            results_ok.append(f"✅ {html.escape(title)}: id={result['channel_id']}")
        done_ops += 1
        global_idx += 1
        try:
            await progress_msg.edit_text(
                _progress_text("Создание каналов...", done_ops, total_ops, len(results_ok), len(results_err)),
                parse_mode="HTML",
            )
        except Exception:
            pass
        # Smart anti-flood: rotate attempt counter, apply human-like delay
        if attempt >= 4:
            attempt = 0
        else:
            attempt += 1
        flood = result.get("flood_wait", 0)
        # Every 5th operation → longer cooldown (simulate human taking a break)
        if (task_i + 1) % 5 == 0:
            base_delay = 120.0  # longer pause every 5 operations
        else:
            base_delay = _human_delay(25, 40)
        chaos = session_simulator.chaos_factor()
        await asyncio.sleep(max(_backoff(attempt, base=2.0, cap=30.0), flood, base_delay * chaos))

    lines = ["🔁 <b>Результаты массового создания</b>\n"]
    lines += results_ok + results_err
    await progress_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK POST TO MULTIPLE CHANNELS
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "bulk_post_chans"))
async def cb_bulk_post_chans_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Пост в каналы — STARTER</b>\n\nОформите: /subscription",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await callback.message.edit_text(
            "❌ Нет подключённых аккаунтов.\n\n/ops → Подключить аккаунт",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in active:
        label = acc["first_name"] or acc["phone"] or f"id={acc['id']}"
        kb.button(
            text=f"👤 {label}",
            callback_data=ChanCb(action="bulk_post_chans_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📤 <b>Пост в несколько каналов</b>\n\nВыберите аккаунт — загружу его каналы:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "bulk_post_chans_acc"))
async def cb_bulk_post_chans_acc(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer("⏳ Загружаю каналы...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    channels = [d for d in (dialogs or []) if d.get("type") in ("channel", "megagroup", "supergroup")]
    if not channels:
        await callback.message.edit_text(
            "❌ У этого аккаунта нет каналов/групп.",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(bpchans_acc_id=acc["id"], bpchans_channels=channels, bpchans_selected=[], bpchans_page=0)
    await state.set_state(BulkPostChansFSM.choosing_channels)
    await _show_bpchans_page(callback.message, state, edit=True)


async def _show_bpchans_page(msg, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    channels = data.get("bpchans_channels", [])
    selected = set(data.get("bpchans_selected", []))
    page = data.get("bpchans_page", 0)
    per_page = 8
    total_pages = max(1, (len(channels) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = channels[start: start + per_page]

    kb = InlineKeyboardBuilder()
    for ch in chunk:
        cid = ch["id"]
        title = (ch.get("title") or f"id={cid}")[:30]
        mark = "✅ " if cid in selected else ""
        kb.button(
            text=f"{mark}{title}",
            callback_data=f"chan:cpsel:{data['bpchans_acc_id']}:{cid}",
        )
    nav_btns = []
    if page > 0:
        nav_btns.append(("◀️", f"chan:cppage:{page - 1}"))
    if page < total_pages - 1:
        nav_btns.append(("▶️", f"chan:cppage:{page + 1}"))
    for label, cbd in nav_btns:
        kb.button(text=label, callback_data=cbd)

    n_sel = len(selected)
    if n_sel:
        kb.button(
            text=f"▶️ Продолжить ({n_sel} канал(ов))",
            callback_data=f"chan:cpsdone:{data['bpchans_acc_id']}",
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="bulk_post_chans"))
    kb.adjust(1)

    text = (
        f"📤 <b>Выберите каналы для поста</b>\n"
        f"Стр. {page + 1}/{total_pages} · Выбрано: {n_sel}\n\n"
        "Нажмите на канал для выбора/снятия:"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("chan:cpsel:"))
async def cb_bpchans_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    ch_id = int(parts[3])
    data = await state.get_data()
    selected = list(data.get("bpchans_selected", []))
    if ch_id in selected:
        selected.remove(ch_id)
    else:
        selected.append(ch_id)
    await state.update_data(bpchans_selected=selected)
    await _show_bpchans_page(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("chan:cppage:"))
async def cb_bpchans_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = int(callback.data.split(":")[2])
    await state.update_data(bpchans_page=page)
    await _show_bpchans_page(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("chan:cpsdone:"))
async def cb_bpchans_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("bpchans_selected", [])
    if not selected:
        await callback.answer("Выберите хотя бы один канал.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BulkPostChansFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await callback.message.edit_text(
        f"📝 <b>Введите текст поста</b>\n\nВыбрано каналов: {len(selected)}\n\nПоддерживается HTML-разметка:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(BulkPostChansFSM.waiting_text)
async def fsm_bpchans_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("⚠️ Введите текст поста:")
        return
    data = await state.get_data()
    selected_ids = data.get("bpchans_selected", [])
    acc_id = data.get("bpchans_acc_id")
    channels = data.get("bpchans_channels", [])
    await state.clear()

    acc = await db.get_account_for_telethon(pool, acc_id, message.from_user.id)
    if not acc:
        await message.answer("❌ Аккаунт не найден.")
        return

    ch_map = {ch["id"]: ch for ch in channels}
    from services import account_manager
    from database import db as _db
    total = len(selected_ids)
    ok, err = 0, 0
    attempt = 0
    progress_msg = await message.answer(
        _progress_text("Публикация постов...", 0, total, 0, 0),
        parse_mode="HTML",
    )
    for idx, ch_id in enumerate(selected_ids, 1):
        access_hash = ch_map.get(ch_id, {}).get("access_hash", 0) or 0
        result = await account_manager.post_to_channel(acc["session_str"], ch_id, text, access_hash=access_hash, _acc=acc)
        if result.get("banned"):
            await _db.deactivate_account(pool, acc_id, "banned detected in bulk op")
            err += 1
        elif "error" in result:
            err += 1
        else:
            ok += 1
        try:
            await progress_msg.edit_text(
                _progress_text("Публикация постов...", idx, total, ok, err),
                parse_mode="HTML",
            )
        except Exception:
            pass
        # Exponential backoff; reset every 5 iterations
        if attempt >= 4:
            attempt = 0
        else:
            attempt += 1
        flood = result.get("flood_wait", 0)
        await asyncio.sleep(max(_backoff(attempt, base=2.0, cap=30.0), flood))

    ch_titles = [ch_map.get(cid, {}).get("title", f"id={cid}") for cid in selected_ids]
    lines = [f"📤 <b>Результаты публикации</b>\n", f"Каналов: {total} · ✅ {ok} · ❌ {err}\n"]
    await progress_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )



# ══════════════════════════════════════════════════════════════════════════
# JOIN CHANNEL
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "join"))
async def cb_join_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Вступление в каналы — STARTER</b>\n\nОформить: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if len(active) == 1:
        acc = await pool.fetchrow(
            "SELECT id, session_str FROM tg_accounts WHERE id=$1", active[0]["id"]
        )
        await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
        await _start_join_fsm(callback.message, state, edit=True)
        return
    kb = _account_picker_kb(active, "join_acc")
    await callback.message.edit_text(
        "🔗 <b>Вступить в канал</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "join_acc"))
async def cb_join_account_chosen(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
    await _start_join_fsm(callback.message, state, edit=True)


async def _start_join_fsm(msg, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(JoinChannelFSM.waiting_invite)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    text = (
        "🔗 <b>Вступить в канал</b>\n\n"
        "Введите username канала или ссылку:\n"
        "• <code>@channelname</code>\n"
        "• <code>https://t.me/channelname</code>\n"
        "• <code>https://t.me/+AbcPrivateHash</code>"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# fsm_join_invite is now handled by fsm_join_invite_combined below (supports bulk selection)


# ══════════════════════════════════════════════════════════════════════════
# LEAVE CHANNEL
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "leave_pick"))
async def cb_leave_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text("🔒 /subscription")
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "leave_dialogs")
    await callback.message.edit_text(
        "🚪 <b>Выйти из канала</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "leave_dialogs"))
async def cb_leave_show_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю список каналов...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.message.edit_text("❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return
    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], limit=30, _acc=acc)
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ Нет доступных каналов/групп.",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(action="do_leave", acc_id=callback_data.acc_id, channel_id=d["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🚪 <b>Выберите канал для выхода:</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "do_leave"))
async def cb_do_leave(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Выхожу...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    from services import account_manager
    ok = await account_manager.leave_channel(acc["session_str"], callback_data.channel_id, _acc=acc)
    if ok:
        await callback.message.edit_text(
            "✅ <b>Вышел из канала</b>",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Не удалось выйти</b>\n\nВозможно, вы уже не являетесь участником.",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )


# ══════════════════════════════════════════════════════════════════════════
# POST TO CHANNEL
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "post_pick"))
async def cb_post_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text("🔒 /subscription")
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "post_dialogs")
    await callback.message.edit_text(
        "📤 <b>Опубликовать пост</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "post_dialogs"))
async def cb_post_show_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer("⏳ Загружаю каналы...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.message.edit_text("❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return
    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], limit=30, _acc=acc)
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ Нет доступных каналов/групп.",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(acc_id=callback_data.acc_id)
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(action="post_channel", acc_id=callback_data.acc_id, channel_id=d["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📤 <b>Выберите канал для публикации:</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "post_channel"))
async def cb_post_channel_chosen(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(acc_id=callback_data.acc_id, channel_id=callback_data.channel_id)
    await state.set_state(PostToChannelFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "📝 <b>Текст публикации</b>\n\nВведите текст поста (поддерживается HTML):",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


    # Single-account post is now handled by fsm_bulk_post_text below (bulk=False path)


# ══════════════════════════════════════════════════════════════════════════
# MANAGE CHANNEL (title / about / username / invite link / delete)
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "manage_pick"))
async def cb_manage_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "manage_dialogs")
    await callback.message.edit_text(
        "✏️ <b>Управление каналом</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "manage_dialogs"))
async def cb_manage_show_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю каналы...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.message.edit_text("❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return
    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], limit=30, _acc=acc)
    if not dialogs:
        await callback.message.edit_text(
            "ℹ️ Нет доступных каналов/групп.", reply_markup=_back_kb().as_markup()
        )
        return
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(action="manage_channel", acc_id=callback_data.acc_id, channel_id=d["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "✏️ <b>Выберите канал:</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "manage_channel"))
async def cb_manage_channel_menu(
    callback: CallbackQuery, callback_data: ChanCb
) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    ch_id = callback_data.channel_id
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить название",   callback_data=ChanCb(action="edit_title",    acc_id=acc_id, channel_id=ch_id))
    kb.button(text="📄 Изменить описание",    callback_data=ChanCb(action="edit_about",    acc_id=acc_id, channel_id=ch_id))
    kb.button(text="🔤 Установить username",  callback_data=ChanCb(action="edit_uname",    acc_id=acc_id, channel_id=ch_id))
    kb.button(text="🔗 Ссылка-приглашение",  callback_data=ChanCb(action="get_invite",    acc_id=acc_id, channel_id=ch_id))
    kb.button(text="👑 Со-Администраторы",   callback_data=ChanCb(action="manage_admins", acc_id=acc_id, channel_id=ch_id))
    kb.button(text="🗑 Удалить канал",        callback_data=ChanCb(action="del_channel",   acc_id=acc_id, channel_id=ch_id))
    kb.button(text="◀️ Назад",               callback_data=ChanCb(action="manage_pick"))
    kb.adjust(2, 2, 2, 1)
    await callback.message.edit_text(
        f"✏️ <b>Управление каналом</b>\n\nID: <code>{ch_id}</code>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )



@router.callback_query(ChanCb.filter(F.action == "edit_title"))
async def cb_edit_title(callback: CallbackQuery, callback_data: ChanCb, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditChannelFSM.waiting_value)
    await state.update_data(field="title", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text("✏️ Введите новое <b>название</b>:", parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanCb.filter(F.action == "edit_about"))
async def cb_edit_about(callback: CallbackQuery, callback_data: ChanCb, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditChannelFSM.waiting_value)
    await state.update_data(field="about", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text("📄 Введите новое <b>описание</b>:", parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanCb.filter(F.action == "edit_uname"))
async def cb_edit_uname(callback: CallbackQuery, callback_data: ChanCb, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditChannelFSM.waiting_value)
    await state.update_data(field="username", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🔤 Введите новый <b>username</b> (без @, только a-z, 0-9, _):",
        parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.message(EditChannelFSM.waiting_value)
async def fsm_edit_value(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account_for_telethon(pool, data.get("acc_id"), message.from_user.id)
    if not acc:
        await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
        return
    from services import account_manager
    field = data["field"]
    ch_id = data["channel_id"]
    kb = _back_kb()
    if field == "title":
        ok = await account_manager.edit_channel_title(acc["session_str"], ch_id, value, _acc=acc)
        await message.answer(
            "✅ Название изменено!" if ok else "❌ Ошибка изменения названия.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    elif field == "about":
        ok = await account_manager.edit_channel_about(acc["session_str"], ch_id, value, _acc=acc)
        await message.answer(
            "✅ Описание изменено!" if ok else "❌ Ошибка изменения описания.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    elif field == "username":
        err = await account_manager.set_channel_username(acc["session_str"], ch_id, value, _acc=acc)
        if err:
            await message.answer(
                f"❌ Ошибка: <code>{html.escape(err)}</code>",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )
        else:
            await message.answer(
                f"✅ Username установлен: @{html.escape(value)}",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )


@router.callback_query(ChanCb.filter(F.action == "get_invite"))
async def cb_get_invite(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Получаю ссылку...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    from services import account_manager
    link = await account_manager.get_channel_invite_link(
        acc["session_str"], callback_data.channel_id, _acc=acc
    )
    if link:
        await callback.message.edit_text(
            f"🔗 <b>Ссылка-приглашение</b>\n\n<code>{html.escape(link)}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(callback_data.acc_id).as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ Не удалось получить ссылку. Проверьте права аккаунта.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )


@router.callback_query(ChanCb.filter(F.action == "manage_admins"))
async def cb_manage_admins(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    """Show managed accounts and let owner promote them to admin in the channel."""
    await callback.answer()
    acc_id = callback_data.acc_id
    ch_id = callback_data.channel_id
    owner_id = callback.from_user.id

    accounts = await pool.fetch(
        "SELECT id, phone, first_name, username, tg_user_id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND tg_user_id IS NOT NULL AND id != $2 "
        "ORDER BY trust_score DESC NULLS LAST",
        owner_id, acc_id,
    )

    kb = InlineKeyboardBuilder()
    lines = [
        "👑 <b>Со-Администраторы</b>\n",
        "Выберите аккаунты для промоции в администраторы канала.\n",
        f"Канал ID: <code>{ch_id}</code>\n",
    ]

    if not accounts:
        lines.append("<i>Нет других активных аккаунтов с известным Telegram ID.</i>")
    else:
        lines.append(f"Доступно {len(accounts)} аккаунтов:")
        for acc in accounts:
            name = (acc["first_name"] or "").strip()
            uname = f"@{acc['username']}" if acc.get("username") else acc.get("phone", "")
            label = f"{name} ({uname})" if name else uname
            kb.button(
                text=f"👑 Промовать: {label}",
                callback_data=ChanCb(action="do_promote", acc_id=acc_id, channel_id=ch_id, page=acc["id"]),
            )
        kb.button(
            text="👑 Промовать ВСЕХ",
            callback_data=ChanCb(action="promote_all", acc_id=acc_id, channel_id=ch_id),
        )

    kb.button(text="◀️ Назад", callback_data=ChanCb(action="manage_channel", acc_id=acc_id, channel_id=ch_id))
    kb.adjust(1)
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanCb.filter(F.action == "do_promote"))
async def cb_do_promote(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    """Promote a single account to admin using the owner's account."""
    await callback.answer("⏳ Промовую в администраторы...")
    acc_id = callback_data.acc_id      # owner account (has admin rights)
    target_db_id = callback_data.page  # target account db id
    ch_id = callback_data.channel_id
    owner_id = callback.from_user.id

    owner_acc = await db.get_account_for_telethon(pool, acc_id, owner_id)
    if not owner_acc:
        await callback.answer("Аккаунт-администратор не найден.", show_alert=True)
        return

    target = await pool.fetchrow(
        "SELECT id, phone, first_name, tg_user_id FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        target_db_id, owner_id,
    )
    if not target or not target["tg_user_id"]:
        await callback.answer("Целевой аккаунт не найден или нет Telegram ID.", show_alert=True)
        return

    from services import account_manager
    ok = await account_manager.promote_to_admin(
        owner_acc["session_str"], ch_id, target["tg_user_id"], _acc=owner_acc
    )
    name = (target["first_name"] or target["phone"] or f"id{target['id']}")
    if ok:
        await callback.answer(f"✅ {name} теперь администратор!", show_alert=True)
    else:
        await callback.answer(f"❌ Не удалось промовать {name}. Проверьте: аккаунт должен быть участником канала.", show_alert=True)


@router.callback_query(ChanCb.filter(F.action == "promote_all"))
async def cb_promote_all(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    """Promote all managed accounts to admin using the owner's account."""
    await callback.answer("⏳ Промовую всех аккаунтов...")
    acc_id = callback_data.acc_id
    ch_id = callback_data.channel_id
    owner_id = callback.from_user.id

    owner_acc = await db.get_account_for_telethon(pool, acc_id, owner_id)
    if not owner_acc:
        await callback.answer("Аккаунт-администратор не найден.", show_alert=True)
        return

    accounts = await pool.fetch(
        "SELECT id, phone, first_name, tg_user_id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND tg_user_id IS NOT NULL AND id != $2",
        owner_id, acc_id,
    )

    from services import account_manager
    ok_count = 0
    fail_count = 0
    for acc in accounts:
        try:
            ok = await account_manager.promote_to_admin(
                owner_acc["session_str"], ch_id, acc["tg_user_id"], _acc=owner_acc
            )
            if ok:
                ok_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
        await asyncio.sleep(2)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="manage_admins", acc_id=acc_id, channel_id=ch_id))
    await callback.message.edit_text(
        f"👑 <b>Промоция завершена</b>\n\n"
        f"✅ Успешно: <b>{ok_count}</b>\n"
        f"❌ Ошибки: <b>{fail_count}</b>\n\n"
        f"<i>Аккаунты должны быть участниками канала перед промоцией.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "del_channel"))
async def cb_del_channel_confirm(
    callback: CallbackQuery, callback_data: ChanCb
) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 ДА, УДАЛИТЬ НАВСЕГДА",
        callback_data=ChanCb(action="do_delete", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id),
    )
    kb.button(text="◀️ Отмена", callback_data=ChanCb(action="manage_channel", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>Удалить канал?</b>\n\nID: <code>{callback_data.channel_id}</code>\n\n"
        "Это действие <b>необратимо</b>. Все сообщения будут удалены.",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "do_delete"))
async def cb_do_delete(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Удаляю...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    from services import account_manager
    ok = await account_manager.delete_channel(acc["session_str"], callback_data.channel_id, _acc=acc)
    await callback.message.edit_text(
        "✅ <b>Канал удалён.</b>" if ok else "❌ <b>Ошибка удаления.</b> Проверьте права.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# MEMBERS
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "members_pick"))
async def cb_members_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Управление участниками — STARTER</b>\n\nОформите: /subscription",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "members_dialogs")
    await callback.message.edit_text(
        "👥 <b>Участники</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_dialogs"))
async def cb_members_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю каналы...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], limit=30, _acc=acc)
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(action="members_menu", acc_id=callback_data.acc_id, channel_id=d["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👥 <b>Выберите канал/группу:</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_menu"))
async def cb_members_menu(
    callback: CallbackQuery, callback_data: ChanCb
) -> None:
    await callback.answer()
    acc_id, ch_id = callback_data.acc_id, callback_data.channel_id
    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Просмотр участников",  callback_data=ChanCb(action="members_view",   acc_id=acc_id, channel_id=ch_id))
    kb.button(text="➕ Пригласить",            callback_data=ChanCb(action="members_invite", acc_id=acc_id, channel_id=ch_id))
    kb.button(text="🚫 Кикнуть пользователя", callback_data=ChanCb(action="members_kick",   acc_id=acc_id, channel_id=ch_id))
    kb.button(text="◀️ Назад",                callback_data=ChanCb(action="members_pick"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"👥 <b>Управление участниками</b>\n\nID канала: <code>{ch_id}</code>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_view"))
async def cb_members_view(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю участников...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.message.edit_text("❌ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return
    from services import account_manager
    members = await account_manager.get_channel_members(
        acc["session_str"], callback_data.channel_id, limit=30, _acc=acc
    )
    if not members:
        await callback.message.edit_text(
            "ℹ️ Нет участников или нет доступа к списку.",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    lines = [f"👥 <b>Участники ({len(members)}):</b>\n"]
    for m in members:
        uname = f"@{html.escape(m['username'])}" if m["username"] else ""
        name = html.escape(m["first_name"])
        bot_tag = " 🤖" if m["is_bot"] else ""
        lines.append(f"• {name} {uname}{bot_tag} — <code>{m['user_id']}</code>")
    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад",
        callback_data=ChanCb(action="members_menu", acc_id=callback_data.acc_id, channel_id=callback_data.channel_id),
    )
    await callback.message.edit_text(
        "\n".join(lines[:35]),
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "members_invite"))
async def cb_members_invite(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(InviteUsersFSM.waiting_usernames)
    await state.update_data(acc_id=callback_data.acc_id, channel_id=callback_data.channel_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "➕ <b>Пригласить пользователей</b>\n\n"
        "Введите username'ы через запятую или по одному на строку:\n"
        "<code>@user1, @user2, @user3</code>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(InviteUsersFSM.waiting_usernames)
async def fsm_invite_usernames(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    raw = (message.text or "").replace(",", "\n")
    usernames = [u.strip() for u in raw.split("\n") if u.strip()]
    data = await state.get_data()
    await state.clear()
    if not usernames:
        await message.answer("⚠️ Список пуст. Начните заново: /ops")
        return
    acc = await db.get_account_for_telethon(pool, data.get("acc_id"), message.from_user.id)
    if not acc:
        await message.answer("⚠️ Аккаунт не найден.")
        return
    msg = await message.answer(f"⏳ Приглашаю {len(usernames)} пользователей...")
    from services import account_manager
    result = await account_manager.invite_users_to_channel(
        acc["session_str"], data["channel_id"], usernames, _acc=acc
    )
    lines = [f"✅ Приглашено: <b>{result['invited']}</b>"]
    if result["failed"]:
        lines.append(f"❌ Ошибки ({len(result['failed'])}):")
        for f_item in result["failed"][:5]:
            lines.append(f"  • {html.escape(f_item)}")
    await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())


@router.callback_query(ChanCb.filter(F.action == "members_kick"))
async def cb_members_kick(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(InviteUsersFSM.waiting_channel_id)  # reuse state for kick
    await state.update_data(
        acc_id=callback_data.acc_id,
        channel_id=callback_data.channel_id,
        action="kick",
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🚫 <b>Кикнуть пользователя</b>\n\nВведите Telegram ID пользователя (число):",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(InviteUsersFSM.waiting_channel_id)
async def fsm_kick_user_id(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Введите числовой Telegram ID.")
        return
    acc = await db.get_account_for_telethon(pool, data.get("acc_id"), message.from_user.id)
    if not acc:
        await message.answer("⚠️ Аккаунт не найден.")
        return
    from services import account_manager
    ok = await account_manager.kick_from_channel(acc["session_str"], data["channel_id"], user_id, _acc=acc)
    await message.answer(
        f"✅ Пользователь <code>{user_id}</code> удалён." if ok
        else f"❌ Не удалось удалить <code>{user_id}</code>. Проверьте права.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# ACCOUNT PROFILE
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "profile_pick"))
async def cb_profile_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text("🔒 /subscription")
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "profile_menu")
    await callback.message.edit_text(
        "🙋 <b>Профиль аккаунта</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "profile_menu"))
async def cb_profile_menu(
    callback: CallbackQuery, callback_data: ChanCb
) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить имя",    callback_data=ChanCb(action="prof_name",  acc_id=acc_id))
    kb.button(text="📝 Изменить bio",    callback_data=ChanCb(action="prof_bio",   acc_id=acc_id))
    kb.button(text="🔤 Изменить username", callback_data=ChanCb(action="prof_uname", acc_id=acc_id))
    kb.button(text="◀️ Назад",           callback_data=ChanCb(action="profile_pick"))
    kb.adjust(2, 1, 1)
    await callback.message.edit_text(
        "🙋 <b>Профиль аккаунта</b>\n\nВыберите что изменить:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


for _prof_action, _prof_field, _prof_prompt in [
    ("prof_name",  "first_name", "✏️ Введите новое <b>имя</b>:"),
    ("prof_bio",   "about",      "📝 Введите новое <b>bio</b> (до 70 символов):"),
    ("prof_uname", "username",   "🔤 Введите новый <b>username</b> аккаунта (без @):"),
]:
    def _make_prof_handler(prof_field, prof_prompt):
        async def _prof_handler(callback: CallbackQuery, callback_data: ChanCb, state: FSMContext):
            await callback.answer()
            await state.set_state(UpdateProfileFSM.waiting_value)
            await state.update_data(field=prof_field, acc_id=callback_data.acc_id)
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
            await callback.message.edit_text(prof_prompt, parse_mode="HTML", reply_markup=kb.as_markup())
        return _prof_handler

    router.callback_query(ChanCb.filter(F.action == _prof_action))(_make_prof_handler(_prof_field, _prof_prompt))


    # Single-account profile update is now handled by fsm_update_profile below (bulk=False path)


# ══════════════════════════════════════════════════════════════════════════
# CREATE BOT VIA BOTFATHER
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "botfather_pick"))
async def cb_botfather_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Создание бота — PRO</b>\n\nОформите: /subscription",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    await state.update_data(bulk_op="botfather", bulk_selected=[])
    await _show_bulk_select(callback, pool, "botfather", set())


@router.message(CreateBotFSM.waiting_count)
async def fsm_botfather_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 5):
        await message.answer("⚠️ Введите число от 1 до 5:")
        return
    await state.update_data(bot_count=int(raw))
    await state.set_state(CreateBotFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await message.answer(
        "🤖 <b>Создание ботов</b>\n\n"
        "Введите <b>отображаемое имя</b> бота (одинаковое для всех):\n\n"
        "Например: <i>My Sales Bot</i>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(CreateBotFSM.waiting_name)
async def fsm_botfather_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("⚠️ Имя от 1 до 64 символов:")
        return
    await state.update_data(bot_name=name)
    await state.set_state(CreateBotFSM.waiting_username)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
    await message.answer(
        f"🤖 Имя: <b>{html.escape(name)}</b>\n\n"
        "Введите <b>базовый username</b> бота.\n"
        "Для нескольких ботов будет добавляться порядковый номер (например: <i>mysalesbot</i>, <i>mysalesbot2</i>):\n\n"
        "Например: <i>mysalesbot</i>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(CreateBotFSM.waiting_username)
async def fsm_botfather_username(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    base_username = (message.text or "").strip().lstrip("@")
    if not base_username or len(base_username) < 5:
        await message.answer("⚠️ Username минимум 5 символов.")
        return
    data = await state.get_data()
    await state.clear()

    selected_ids = data.get("bulk_selected", [])
    bot_count = data.get("bot_count", 1)

    # Fallback to single-account mode (legacy path, shouldn't normally trigger)
    if not selected_ids and data.get("acc_id"):
        selected_ids = [data["acc_id"]]

    accounts = await pool.fetch(
        "SELECT a.id, a.session_str, a.first_name, a.phone, "
        "a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
        message.from_user.id, selected_ids,
    )
    if not accounts:
        await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
        return

    total = len(accounts) * bot_count
    msg = await message.answer(
        _progress_text("Создание ботов...", 0, total, 0, 0),
        parse_mode="HTML",
    )

    from services import account_manager
    from database import db as _db
    results_ok, results_err = [], []
    done_ops = 0
    attempt = 0
    active_accounts = list(accounts)

    # Round-robin: task global_i uses active_accounts[global_i % len(active_accounts)]
    for global_i in range(total):
        if not active_accounts:
            results_err.append("❌ Нет доступных аккаунтов")
            done_ops += 1
            continue
        acc_idx = global_i % len(active_accounts)
        acc = active_accounts[acc_idx]
        acc_label = html.escape(acc["first_name"] or acc["phone"])
        # Determine suffix: overall bot index across all accounts
        suffix = str(global_i + 1) if (total > 1) else ""
        username = (base_username.rstrip("bot") + (suffix if suffix else "") + "bot") if base_username.endswith("bot") else (base_username + suffix)

        # Account rotation on banned/flood_wait
        tried_accs: set[int] = set()
        result = None
        for candidate in active_accounts:
            if candidate["id"] in tried_accs:
                continue
            tried_accs.add(candidate["id"])
            result = await account_manager.create_bot_via_botfather(
                candidate["session_str"], data["bot_name"], username, _acc=dict(candidate)
            )
            if result.get("banned"):
                await _db.deactivate_account(pool, candidate["id"], "banned detected in bulk op")
                active_accounts = [a for a in active_accounts if a["id"] != candidate["id"]]
                continue
            if result.get("flood_wait"):
                continue
            break
        if result is None:
            result = {"error": "нет доступных аккаунтов"}

        if "error" in result:
            results_err.append(f"❌ {acc_label} [{username}]: {html.escape(result['error'][:60])}")
        else:
            token = result["token"]
            results_ok.append(
                f"✅ {acc_label}: @{html.escape(result['username'])} — <code>{token}</code>"
            )
        done_ops += 1
        try:
            await msg.edit_text(
                _progress_text("Создание ботов...", done_ops, total, len(results_ok), len(results_err)),
                parse_mode="HTML",
            )
        except Exception:
            pass
        # Exponential backoff; reset every 5 iterations
        if attempt >= 4:
            attempt = 0
        else:
            attempt += 1
        flood = result.get("flood_wait", 0)
        await asyncio.sleep(max(_backoff(attempt, base=2.0, cap=60.0), flood))

    lines = [f"🤖 <b>Результаты создания ботов</b> ({len(results_ok)}/{total})\n"]
    lines += results_ok + results_err
    await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())


@router.callback_query(F.data.startswith("add_bot_token:"))
async def cb_add_bot_token(
    callback: CallbackQuery, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await callback.answer()
    token = callback.data.split(":", 1)[1]
    from database import db as _db
    from services import bot_api as _bot_api
    from bot.keyboards import bot_menu
    progress = await callback.message.answer("⏳ Добавляю бота...")
    bot_info = await _bot_api.get_me(http, token)
    if not bot_info:
        await progress.edit_text("❌ Не удалось получить информацию о боте. Токен недействителен.")
        return
    added = await _db.add_bot(
        pool, token=token, bot_id=bot_info["id"],
        username=bot_info.get("username", ""),
        first_name=bot_info.get("first_name", ""),
        added_by=callback.from_user.id,
    )
    safe = (bot_info.get("username") or bot_info.get("first_name", "")).replace("&", "&amp;")
    if added:
        await progress.edit_text(
            f"✅ Бот @{safe} добавлен в платформу!",
            parse_mode="HTML",
            reply_markup=bot_menu(bot_info["id"], username=bot_info.get("username")),
        )
    else:
        await progress.edit_text(
            f"⚠️ Бот @{safe} уже добавлен в вашу платформу.",
            parse_mode="HTML",
        )


# ══════════════════════════════════════════════════════════════════════════
# REACTIONS
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "react_pick"))
async def cb_react_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text("🔒 /subscription")
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "react_dialogs")
    await callback.message.edit_text(
        "👍 <b>Реакция на пост</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "react_dialogs"))
async def cb_react_dialogs(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю каналы...")
    acc = await db.get_account_for_telethon(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], limit=30, _acc=acc)
    await state.update_data(acc_id=callback_data.acc_id)
    kb = InlineKeyboardBuilder()
    for d in dialogs[:20]:
        label = f"{'📢' if d['type'] == 'channel' else '👥'} {d['title'][:30]}"
        kb.button(
            text=label,
            callback_data=ChanCb(action="react_channel", acc_id=callback_data.acc_id, channel_id=d["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👍 <b>Выберите канал:</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "react_channel"))
async def cb_react_channel(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(SendReactionFSM.waiting_msg_id)
    await state.update_data(acc_id=callback_data.acc_id, channel_id=callback_data.channel_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "👍 <b>ID сообщения</b>\n\nВведите ID сообщения, на которое хотите поставить реакцию:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(SendReactionFSM.waiting_msg_id)
async def fsm_react_msg_id(message: Message, state: FSMContext) -> None:
    try:
        msg_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Введите числовой ID сообщения.")
        return
    await state.update_data(msg_id=msg_id)
    await state.set_state(SendReactionFSM.choosing_emoji)
    kb = InlineKeyboardBuilder()
    for emoji in REACTION_EMOJIS:
        kb.button(text=emoji, callback_data=f"chan:do_react:{emoji}")
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(5, 5, 1)
    await message.answer(
        "👍 <b>Выберите реакцию:</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("chan:do_react:"))
async def cb_do_react(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    parts = callback.data.split(":", 2)
    emoji = parts[2] if len(parts) >= 3 else "👍"
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account_for_telethon(pool, data.get("acc_id"), callback.from_user.id)
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.")
        return
    from services import account_manager
    ok = await account_manager.send_reaction(
        acc["session_str"], data["channel_id"], data["msg_id"], emoji, _acc=acc
    )
    await callback.message.edit_text(
        f"✅ Реакция {emoji} отправлена!" if ok else "❌ Ошибка отправки реакции.",
        parse_mode="HTML", reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# REPORT CONTENT
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "report_pick"))
async def cb_report_pick_account(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    kb = _account_picker_kb(active, "report_start")
    await callback.message.edit_text(
        "🚨 <b>Пожаловаться на контент</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "report_start"))
async def cb_report_account_chosen(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(ReportFSM.waiting_peer)
    await state.update_data(acc_id=callback_data.acc_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "🚨 <b>Жалоба</b>\n\nВведите username канала/пользователя:\n<code>@username</code>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(ReportFSM.waiting_peer)
async def fsm_report_peer(message: Message, state: FSMContext) -> None:
    peer = (message.text or "").strip()
    await state.update_data(peer=peer)
    await state.set_state(ReportFSM.choosing_reason)
    kb = InlineKeyboardBuilder()
    for key, label in REPORT_REASONS.items():
        kb.button(text=label, callback_data=f"chan:report_reason:{key}")
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="menu"))
    kb.adjust(2, 2, 2, 1)
    await message.answer(
        f"🚨 Жалоба на: <code>{html.escape(peer)}</code>\n\nВыберите причину:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("chan:report_reason:"))
async def cb_report_reason(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    reason = callback.data.split(":", 2)[2] if ":" in callback.data else "spam"
    data = await state.get_data()
    await state.clear()
    acc = await db.get_account_for_telethon(pool, data.get("acc_id"), callback.from_user.id)
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.")
        return
    from services import account_manager
    ok = await account_manager.report_peer(acc["session_str"], data["peer"], reason, _acc=acc)
    label = REPORT_REASONS.get(reason, reason)
    await callback.message.edit_text(
        f"✅ <b>Жалоба отправлена!</b>\n\nПричина: {label}\nОбъект: <code>{html.escape(data['peer'])}</code>"
        if ok else
        "❌ <b>Ошибка отправки жалобы</b>\n\nПроверьте username и попробуйте снова.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK MENU (mass operations across ALL active accounts)
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(ChanCb.filter(F.action == "bulk_menu"))
async def cb_bulk_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Массовые операции — PRO</b>\n\nОформите: /subscription",
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        callback.from_user.id,
    )
    count = len(accounts)
    await callback.message.edit_text(
        f"⚡ <b>Массовые операции</b>\n\n"
        f"Активных аккаунтов: <b>{count}</b>\n\n"
        "Выберите операцию — затем выберете конкретные аккаунты (или все сразу):\n"
        "• 📢 Создать канал/группу — создаст на выбранных аккаунтах\n"
        "• 🔗 Вступить в канал — все выбранные вступят по ссылке\n"
        "• 🚪 Выйти из канала — все выбранные покинут канал\n"
        "• 📤 Опубликовать пост — опубликует от всех выбранных\n"
        "• ✏️ Имя / 📝 Bio / 🔤 Username — изменить профиль аккаунтов\n\n"
        "💡 После выбора операции появится список аккаунтов с чекбоксами",
        parse_mode="HTML",
        reply_markup=_bulk_menu_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK ACCOUNT SELECTION (toggles → confirm → execute)
# ══════════════════════════════════════════════════════════════════════════

async def _show_bulk_select(
    msg_or_cb, pool: asyncpg.Pool, op: str, selected: set[int], edit: bool = True
) -> None:
    """Render account selection keyboard for a bulk operation."""
    from aiogram.types import CallbackQuery as _CQ
    is_cb = isinstance(msg_or_cb, _CQ)
    owner_id = msg_or_cb.from_user.id

    accounts = await pool.fetch(
        "SELECT id, first_name, username, phone, is_active FROM tg_accounts "
        "WHERE owner_id=$1 ORDER BY added_at",
        owner_id,
    )
    active = [a for a in accounts if a["is_active"]]

    if not active:
        text = (
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Добавьте аккаунт через 📱 <b>Мои аккаунты</b> в главном меню,\n"
            "или нажмите /accounts"
        )
        kb = _back_kb()
        if is_cb:
            try:
                await msg_or_cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            except Exception:
                await msg_or_cb.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        return

    # Filter selected to only existing active account IDs
    active_ids = {a["id"] for a in active}
    selected = selected & active_ids if selected else active_ids

    op_label = _BULK_OP_LABELS.get(op, op)
    n = len(selected)
    total = len(active)
    text = (
        f"⚡ <b>{op_label}</b>\n\n"
        f"Выбрано: <b>{n}</b> из {total} аккаунтов\n\n"
        "Нажмите на аккаунт чтобы включить/выключить.\n"
        "Когда готово — нажмите <b>▶️ Продолжить</b>."
    )
    kb = _bulk_select_kb(active, selected, op)
    if is_cb:
        try:
            await msg_or_cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await msg_or_cb.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# Entry point for each bulk operation — shows account picker with all accounts pre-selected
@router.callback_query(ChanCb.filter(F.action.in_({"bulk_dm", "bulk_join", "bulk_leave",
                                                    "bulk_post", "bulk_prof_name",
                                                    "bulk_prof_bio", "bulk_prof_uname"})))
async def cb_bulk_start_op(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    op_map = {
        "bulk_dm":         "dm",
        "bulk_join":       "join",
        "bulk_leave":      "leave",
        "bulk_post":       "post",
        "bulk_prof_name":  "prof_name",
        "bulk_prof_bio":   "prof_bio",
        "bulk_prof_uname": "prof_uname",
    }
    op = op_map[callback_data.action]
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", callback.from_user.id
    )
    selected = {a["id"] for a in accounts}  # start with all selected
    await state.update_data(bulk_op=op, bulk_selected=list(selected))
    await _show_bulk_select(callback, pool, op, selected)


# Toggle a single account
@router.callback_query(F.data.startswith("chan:bsel:"))
async def cb_bulk_toggle_acc(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    parts = callback.data.split(":")   # chan, bsel, op, acc_id
    if len(parts) < 4:
        return
    op = parts[2]
    try:
        acc_id = int(parts[3])
    except ValueError:
        return
    data = await state.get_data()
    selected = set(data.get("bulk_selected", []))
    if acc_id in selected:
        selected.discard(acc_id)
    else:
        selected.add(acc_id)
    await state.update_data(bulk_selected=list(selected), bulk_op=op)
    await _show_bulk_select(callback, pool, op, selected)


# Select all accounts
@router.callback_query(F.data.startswith("chan:bsall:"))
async def cb_bulk_select_all(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    op = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", callback.from_user.id
    )
    selected = {a["id"] for a in accounts}
    await state.update_data(bulk_selected=list(selected), bulk_op=op)
    await _show_bulk_select(callback, pool, op, selected)


# Deselect all accounts
@router.callback_query(F.data.startswith("chan:bsnone:"))
async def cb_bulk_select_none(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    op = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    await state.update_data(bulk_selected=[], bulk_op=op)
    await _show_bulk_select(callback, pool, op, set())


# Confirm selection — route to operation-specific input
@router.callback_query(F.data.startswith("chan:bsdone:"))
async def cb_bulk_confirm_selection(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    op = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    if not selected_ids:
        await callback.answer("⚠️ Не выбрано ни одного аккаунта.", show_alert=True)
        return
    await callback.answer()

    # Route to the appropriate input step
    if op == "create":
        await state.update_data(bulk_op=op)
        await state.set_state(BulkCreateFSM.waiting_title)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🔁 <b>Массовое создание</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите <b>название</b> канала/группы:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif op == "dm":
        await state.update_data(bulk_op=op)
        await state.set_state(BulkDmFSM.waiting_usernames)
        n_acc = len(selected_ids)
        # delay per account: 5s single, 3s two, 2.5s three+
        delay_s = 5.0 if n_acc == 1 else (3.0 if n_acc == 2 else 2.5)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"✉️ <b>Рассылка личных сообщений</b>\n\n"
            f"Аккаунтов для отправки: <b>{n_acc}</b>\n"
            f"Задержка между сообщениями: ~<b>{delay_s:.0f}с</b>\n\n"
            "📋 <b>Шаг 1/2 — Список получателей</b>\n\n"
            "Отправьте список username (по одному на строку):\n\n"
            "<code>@username1\n@username2\n@username3</code>\n\n"
            "💡 Символ @ необязателен. Принимаются также числовые ID.\n"
            "⚠️ Рекомендуется не более 200 получателей за сеанс.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif op == "join":
        await state.update_data(bulk_op=op)
        await state.set_state(JoinChannelFSM.waiting_invite)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🔗 <b>Вступить в канал</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите username или ссылку-приглашение:\n"
            "• <code>@channelname</code>\n"
            "• <code>https://t.me/+AbcHash</code>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif op == "leave":
        await state.update_data(bulk_op=op)
        await state.set_state(PostToChannelFSM.waiting_channel_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🚪 <b>Выйти из канала</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите username или числовой ID канала:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif op == "post":
        await state.update_data(bulk_op=op)
        await state.set_state(PostToChannelFSM.waiting_channel_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"📤 <b>Опубликовать пост</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Введите username или числовой ID канала для публикации:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif op == "botfather":
        await state.update_data(bulk_op=op)
        await state.set_state(CreateBotFSM.waiting_count)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"🤖 <b>Создать боты через @BotFather</b>\n\n"
            f"Выбрано аккаунтов: <b>{len(selected_ids)}</b>\n\n"
            "Сколько ботов создать на каждом аккаунте? (1–5):",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif op in ("prof_name", "prof_bio", "prof_uname"):
        field_map = {
            "prof_name":  ("first_name", "✏️ Введите новое <b>имя</b>:"),
            "prof_bio":   ("about",      "📝 Введите новое <b>bio</b> (до 70 символов):"),
            "prof_uname": ("username",   "🔤 Введите <b>username</b> (для 2-го+ аккаунтов добавится цифра):"),
        }
        field, prompt = field_map[op]
        await state.update_data(bulk_op=op, bulk_field=field)
        await state.set_state(UpdateProfileFSM.waiting_value)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await callback.message.edit_text(
            f"{prompt}\n\n<i>Выбрано аккаунтов: {len(selected_ids)}</i>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )


# ── FSM: channel reference input (leave or post) ──────────────────────────

@router.message(PostToChannelFSM.waiting_channel_id)
async def fsm_bulk_channel_id(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    channel_ref = (message.text or "").strip()
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    await state.clear()

    accounts = await pool.fetch(
        "SELECT a.id, a.session_str, a.first_name, a.phone, "
        "a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
        message.from_user.id, selected_ids,
    ) if selected_ids else []

    if not accounts:
        await message.answer("⚠️ Нет выбранных аккаунтов. Начните заново: /ops")
        return

    from services import account_manager

    if op == "leave":
        from database import db as _db
        total = len(accounts)
        msg = await message.answer(
            _progress_text("Покидаю каналы...", 0, total, 0, 0), parse_mode="HTML"
        )
        ok_list, err_list = [], []
        attempt = 0
        for idx, acc in enumerate(accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            result = None
            try:
                result = await account_manager.leave_channel(acc["session_str"], channel_ref, _acc=dict(acc))
            except Exception as e:
                err_list.append(f"❌ {label}: {str(e)[:50]}")
            if result is not None:
                if isinstance(result, dict) and result.get("banned"):
                    await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                    err_list.append(f"❌ {label}: забанен")
                elif isinstance(result, dict) and result.get("flood_wait"):
                    err_list.append(f"⏳ {label}: flood_wait, пропущен")
                elif result:
                    ok_list.append(f"✅ {label}")
                else:
                    err_list.append(f"❌ {label}: не удалось")
            try:
                await msg.edit_text(
                    _progress_text("Покидаю каналы...", idx + 1, total, len(ok_list), len(err_list)),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            # Exponential backoff; reset every 5 iterations
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = (result.get("flood_wait", 0) if isinstance(result, dict) else 0)
            await asyncio.sleep(max(_backoff(attempt, base=2.0, cap=30.0), flood))
        lines = [f"🚪 <b>Выход из {html.escape(channel_ref)}</b>\n"] + ok_list + err_list
        await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())

    elif op == "post":
        await state.update_data(bulk_op=op, bulk_selected=selected_ids, channel_id_ref=channel_ref)
        await state.set_state(PostToChannelFSM.waiting_text)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))
        await message.answer(
            f"📝 Введите <b>текст поста</b> для <code>{html.escape(channel_ref)}</code>:\n\n"
            "<i>Поддерживается HTML-форматирование</i>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )


# ── FSM: post text input ──────────────────────────────────────────────────

@router.message(PostToChannelFSM.waiting_text)
async def fsm_bulk_post_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text_to_post = (message.text or "").strip()
    if not text_to_post:
        await message.answer("⚠️ Введите текст поста:")
        return
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])

    if op == "post" and selected_ids:
        channel_ref = data.get("channel_id_ref", "")
        await state.clear()
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
            message.from_user.id, selected_ids,
        )
        if not accounts:
            await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
            return
        total = len(accounts)
        msg = await message.answer(
            _progress_text("Публикую посты...", 0, total, 0, 0), parse_mode="HTML"
        )
        from services import account_manager
        # Для числового ID канала пробуем найти access_hash в кэше
        bulk_access_hash = 0
        if channel_ref.lstrip("-").isdigit():
            cid = abs(int(channel_ref))
            ah_row = await pool.fetchrow(
                "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
                message.from_user.id, cid,
            )
            bulk_access_hash = (ah_row["access_hash"] if ah_row else 0) or 0
        from database import db as _db
        ok_list, err_list = [], []
        attempt = 0
        active_accounts = list(accounts)
        for idx, acc in enumerate(active_accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            acc_id_cur = acc.get("id")
            result = await account_manager.post_to_channel(acc["session_str"], channel_ref, text_to_post, access_hash=bulk_access_hash, _acc=dict(acc))
            if result.get("banned"):
                if acc_id_cur:
                    await _db.deactivate_account(pool, acc_id_cur, "banned detected in bulk op")
                err_list.append(f"❌ {label}: забанен")
            elif result.get("flood_wait"):
                err_list.append(f"⏳ {label}: flood_wait, пропущен")
            elif "msg_id" in result:
                ok_list.append(f"✅ {label}: msg_id={result['msg_id']}")
            else:
                err_list.append(f"❌ {label}: {html.escape(result.get('error', 'ошибка')[:60])}")
            try:
                await msg.edit_text(
                    _progress_text("Публикую посты...", idx + 1, total, len(ok_list), len(err_list)),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            # Exponential backoff; reset every 5 iterations
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0)
            await asyncio.sleep(max(_backoff(attempt), flood))
        lines = [f"📤 <b>Публикация в {html.escape(channel_ref)}</b>\n"] + ok_list + err_list
        await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())
    else:
        # Single-account post (from cb_post_channel_chosen)
        acc_id = data.get("acc_id")
        ch_id = data.get("channel_id")
        await state.clear()
        acc = await db.get_account_for_telethon(pool, acc_id, message.from_user.id)
        if not acc:
            await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
            return
        msg = await message.answer("⏳ Публикую...")
        from services import account_manager
        single_ah_row = await pool.fetchrow(
            "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
            message.from_user.id, ch_id,
        )
        single_access_hash = (single_ah_row["access_hash"] if single_ah_row else 0) or 0
        result = await account_manager.post_to_channel(acc["session_str"], ch_id, text_to_post, access_hash=single_access_hash, _acc=acc)
        kb = _back_kb()
        if "msg_id" in result:
            await msg.edit_text(
                f"✅ <b>Пост опубликован!</b>\n\nID сообщения: <code>{result['msg_id']}</code>",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )
        else:
            err_detail = html.escape(result.get("error", "неизвестная ошибка")[:120])
            await msg.edit_text(
                f"❌ <b>Ошибка публикации</b>\n\n<code>{err_detail}</code>",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )


# ── FSM: bulk join (uses selected accounts from state) ────────────────────

@router.message(JoinChannelFSM.waiting_invite)
async def fsm_join_invite_combined(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    invite = (message.text or "").strip()
    data = await state.get_data()
    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    is_bulk = op == "join" and bool(selected_ids)
    await state.clear()

    from services import account_manager

    if is_bulk:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
            message.from_user.id, selected_ids,
        )
        if not accounts:
            await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
            return
        from database import db as _db
        total = len(accounts)
        msg = await message.answer(
            _progress_text("Вступаю в канал...", 0, total, 0, 0), parse_mode="HTML"
        )
        ok_list, err_list = [], []
        active_accounts = list(accounts)
        attempt = 0
        # Round-robin: distribute join attempts across accounts
        for idx, acc in enumerate(active_accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            result = await account_manager.join_channel(acc["session_str"], invite, _acc=dict(acc))
            if result.get("banned"):
                await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                err_list.append(f"❌ {label}: забанен")
            elif result.get("flood_wait"):
                err_list.append(f"⏳ {label}: flood_wait, пропущен")
            elif "error" in result:
                err_list.append(f"❌ {label}: {html.escape(result['error'][:60])}")
            else:
                ok_list.append(f"✅ {label}: вступил")
            try:
                await msg.edit_text(
                    _progress_text("Вступаю в канал...", idx + 1, total, len(ok_list), len(err_list)),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            # Exponential backoff; reset every 5 iterations
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            flood = result.get("flood_wait", 0)
            await asyncio.sleep(max(_backoff(attempt), flood))
        lines = [f"🔗 <b>Вступление в {html.escape(invite)}</b>\n"] + ok_list + err_list
        await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())
        return

    # Single-account join
    acc = await db.get_account_for_telethon(pool, data.get("acc_id"), message.from_user.id)
    if not acc:
        await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
        return
    msg = await message.answer("⏳ Вступаю...")
    result = await account_manager.join_channel(acc["session_str"], invite, _acc=acc)
    kb = _back_kb()
    if "error" in result:
        await msg.edit_text(
            f"❌ <b>Ошибка</b>\n\n<code>{html.escape(result['error'])}</code>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    else:
        title = html.escape(result.get("title", ""))
        members = result.get("members", 0)
        await msg.edit_text(
            f"✅ <b>Вступил в канал!</b>\n\n"
            f"Название: <b>{title}</b>\n"
            f"Участников: <b>{members:,}</b>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )


# ── FSM: profile update (single or bulk with selected accounts) ───────────

@router.message(UpdateProfileFSM.waiting_value)
async def fsm_update_profile(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()

    op = data.get("bulk_op", "")
    selected_ids = data.get("bulk_selected", [])
    is_bulk = op.startswith("prof_") and bool(selected_ids)
    field = data.get("bulk_field") or data.get("field", "")

    from services import account_manager

    if is_bulk:
        accounts = await pool.fetch(
            "SELECT a.id, a.session_str, a.first_name, a.phone, "
            "a.device_model, a.system_version, a.app_version, p.proxy_url "
            "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
            "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
            message.from_user.id, selected_ids,
        )
        if not accounts:
            await message.answer("⚠️ Аккаунты не найдены.")
            return
        total = len(accounts)
        msg = await message.answer(
            _progress_text("Обновляю профили...", 0, total, 0, 0), parse_mode="HTML"
        )
        from database import db as _db
        ok_list, err_list = [], []
        attempt = 0
        for i, acc in enumerate(accounts):
            label = html.escape(acc["first_name"] or acc["phone"])
            actual_value = f"{value}{i+1}" if field == "username" and i > 0 else value
            try:
                if field == "username":
                    result = await account_manager.update_account_username(acc["session_str"], actual_value, _acc=dict(acc))
                    if isinstance(result, dict) and result.get("banned"):
                        await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                        err_list.append(f"❌ {label}: забанен")
                    elif isinstance(result, dict) and result.get("flood_wait"):
                        err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    elif result and not isinstance(result, dict):
                        err_list.append(f"❌ {label}: {html.escape(str(result)[:50])}")
                    else:
                        ok_list.append(f"✅ {label}: @{html.escape(actual_value)}")
                else:
                    result = await account_manager.update_profile(acc["session_str"], **{field: value}, _acc=dict(acc))
                    if isinstance(result, dict) and result.get("banned"):
                        await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
                        err_list.append(f"❌ {label}: забанен")
                    elif isinstance(result, dict) and result.get("flood_wait"):
                        err_list.append(f"⏳ {label}: flood_wait, пропущен")
                    elif result:
                        ok_list.append(f"✅ {label}")
                    else:
                        err_list.append(f"❌ {label}: ошибка")
            except Exception as e:
                err_list.append(f"❌ {label}: {str(e)[:50]}")
            try:
                await msg.edit_text(
                    _progress_text("Обновляю профили...", i + 1, total, len(ok_list), len(err_list)),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            # Exponential backoff; reset every 5 iterations
            if attempt >= 4:
                attempt = 0
            else:
                attempt += 1
            await asyncio.sleep(_backoff(attempt, base=2.0, cap=30.0))
        lines = [f"✏️ <b>Обновление {field}</b>\n"] + ok_list + err_list
        await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())
    else:
        acc = await db.get_account_for_telethon(pool, data.get("acc_id"), message.from_user.id)
        if not acc:
            await message.answer("⚠️ Аккаунт не найден.")
            return
        kb = _back_kb()
        if field == "username":
            err = await account_manager.update_account_username(acc["session_str"], value, _acc=acc)
            if err:
                await message.answer(
                    f"❌ Ошибка: <code>{html.escape(err)}</code>",
                    parse_mode="HTML", reply_markup=kb.as_markup(),
                )
            else:
                await message.answer(
                    f"✅ Username обновлён: @{html.escape(value)}",
                    parse_mode="HTML", reply_markup=kb.as_markup(),
                )
        else:
            ok = await account_manager.update_profile(acc["session_str"], **{field: value}, _acc=acc)
            await message.answer(
                "✅ Профиль обновлён!" if ok else "❌ Ошибка обновления профиля.",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )


# ══════════════════════════════════════════════════════════════════════════
# BULK DM — mass direct messages to a username list
# ══════════════════════════════════════════════════════════════════════════

def _parse_username_list(raw: str) -> list[str]:
    """Parse a multiline/comma-separated username list into clean targets."""
    import re
    # split on newlines, commas, semicolons, spaces
    parts = re.split(r"[\n,;]+", raw)
    result = []
    seen: set[str] = set()
    for p in parts:
        p = p.strip().lstrip("@").strip()
        if not p:
            continue
        key = p.lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


@router.message(BulkDmFSM.waiting_usernames)
async def fsm_bulk_dm_usernames(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    usernames = _parse_username_list(raw)
    if not usernames:
        await message.answer(
            "⚠️ Список пустой. Отправьте usernames — по одному на строке:\n"
            "<code>@username1\n@username2</code>",
            parse_mode="HTML",
        )
        return

    await state.update_data(bulk_dm_usernames=usernames)
    await state.set_state(BulkDmFSM.waiting_text)

    data = await state.get_data()
    selected_ids = data.get("bulk_selected", [])
    n_acc = max(len(selected_ids), 1)
    delay_s = 5.0 if n_acc == 1 else (3.0 if n_acc == 2 else 2.5)
    est_min = round(len(usernames) * delay_s / 60, 1)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="bulk_menu"))

    await message.answer(
        f"✅ Получателей: <b>{len(usernames)}</b>\n"
        f"Аккаунтов: <b>{n_acc}</b> | задержка ~{delay_s:.0f}с\n"
        f"Ориентировочное время: ~<b>{est_min}</b> мин\n\n"
        "📝 <b>Шаг 2/2 — Текст сообщения</b>\n\n"
        "Отправьте текст, который будет разослан всем получателям.\n"
        "Поддерживается HTML-форматирование: <b>жирный</b>, <i>курсив</i>, <code>код</code>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkDmFSM.waiting_text)
async def fsm_bulk_dm_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    from services import account_manager
    text_to_send = message.text or message.caption or ""
    if not text_to_send.strip():
        await message.answer("⚠️ Текст не может быть пустым. Отправьте текст сообщения:")
        return

    data = await state.get_data()
    usernames = data.get("bulk_dm_usernames", [])
    selected_ids = data.get("bulk_selected", [])
    await state.clear()

    if not usernames or not selected_ids:
        await message.answer("⚠️ Данные рассылки устарели. Начните заново: /ops")
        return

    accounts = await pool.fetch(
        "SELECT a.id, a.session_str, a.first_name, a.phone, "
        "a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[]) AND a.is_active=TRUE",
        message.from_user.id, selected_ids,
    )
    if not accounts:
        await message.answer("⚠️ Аккаунты не найдены. Начните заново: /ops")
        return

    n_acc = len(accounts)
    # Delay between consecutive sends; per-account delay = n_acc × base_delay
    base_delay = 5.0 if n_acc == 1 else (3.0 if n_acc == 2 else 2.5)
    total = len(usernames)

    progress_msg = await message.answer(
        f"⏳ <b>Рассылка запущена</b>\n\n"
        f"Получателей: <b>{total}</b> | Аккаунтов: <b>{n_acc}</b>\n"
        f"Задержка: ~{base_delay:.0f}с | Ожидаемое время: ~{round(total * base_delay / 60, 1)} мин\n\n"
        "⏳ 0 / " + str(total),
        parse_mode="HTML",
    )

    from database import db as _db
    ok_list: list[str] = []
    err_list: list[str] = []
    flood_wait_total = 0
    active_accounts = list(accounts)

    for i, username in enumerate(usernames):
        if not active_accounts:
            err_list.append(f"❌ @{html.escape(username)}: нет активных аккаунтов")
            continue
        n_active = len(active_accounts)
        acc = active_accounts[i % n_active]
        result = await account_manager.send_dm(acc["session_str"], username, text_to_send, _acc=dict(acc))

        u_escaped = html.escape(username)
        if result.get("banned"):
            await _db.deactivate_account(pool, acc["id"], "banned detected in bulk op")
            active_accounts = [a for a in active_accounts if a["id"] != acc["id"]]
            err_list.append(f"❌ @{u_escaped}: аккаунт забанен")
        elif result.get("flood_wait"):
            # Skip to next account but keep in pool
            flood_wait_total += result.get("flood_wait", 0)
            err_list.append(f"⏳ @{u_escaped}: flood_wait")
        elif result.get("ok"):
            ok_list.append(f"✅ @{u_escaped}")
        else:
            err = html.escape(result.get("error", "неизвестная ошибка")[:60])
            err_list.append(f"❌ @{u_escaped}: {err}")
            # If flood wait from Telegram — add extra wait on top of base delay
            flood_wait_total += result.get("flood_wait", 0)

        if (i + 1) % 5 == 0 or i + 1 == total:
            try:
                await progress_msg.edit_text(
                    _progress_text("Рассылка ЛС...", i + 1, total, len(ok_list), len(err_list)),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Delay: base + any extra flood wait accumulated
        wait = base_delay + min(flood_wait_total, 30)
        flood_wait_total = max(0, flood_wait_total - base_delay)  # drain gradually
        await asyncio.sleep(wait)

    # Final report — split into chunks if too long (Telegram 4096 char limit)
    sent = len(ok_list)
    failed = len(err_list)
    header = (
        f"📊 <b>Рассылка завершена</b>\n\n"
        f"Всего: <b>{total}</b> | ✅ Успешно: <b>{sent}</b> | ❌ Ошибок: <b>{failed}</b>\n\n"
    )

    # Show first 30 errors (most useful for debugging)
    error_section = ""
    if err_list:
        shown_errors = err_list[:30]
        error_section = "<b>Ошибки:</b>\n" + "\n".join(shown_errors)
        if len(err_list) > 30:
            error_section += f"\n<i>...и ещё {len(err_list) - 30} ошибок</i>"

    final_text = header + error_section
    await progress_msg.edit_text(
        final_text,
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# MY CHANNELS — browse channels from connected accounts
# ══════════════════════════════════════════════════════════════════════════

_CHANS_PAGE_SIZE = 8


@router.callback_query(ChanCb.filter(F.action == "my_chans"))
async def cb_my_chans(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Мои каналы — STARTER</b>\n\nОформить: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\nДобавьте через /accounts",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    if len(active) == 1:
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1", active[0]["id"]
        )
        await state.update_data(my_chans_acc_id=acc["id"], my_chans_session=acc["session_str"])
        await state.set_state(MyChannelsFSM.browsing)
        await _show_my_chans_page(
            callback.message, pool, acc["session_str"], acc["id"], page=0, edit=True,
            owner_id=callback.from_user.id, acc_row=dict(acc),
        )
        return
    kb = InlineKeyboardBuilder()
    for a in active:
        kb.button(text=_acc_label(a), callback_data=ChanCb(action="my_chans_acc", acc_id=a["id"]))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    await state.set_state(MyChannelsFSM.choosing_account)
    await callback.message.edit_text(
        "📋 <b>Мои каналы/чаты</b>\n\nВыберите аккаунт для просмотра:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_acc"))
async def cb_my_chans_acc(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    acc = await pool.fetchrow(
        "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id, callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(my_chans_acc_id=acc["id"], my_chans_session=acc["session_str"])
    await state.set_state(MyChannelsFSM.browsing)
    await _show_my_chans_page(
        callback.message, pool, acc["session_str"], acc["id"], page=0, edit=True,
        owner_id=callback.from_user.id, acc_row=dict(acc),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_page"))
async def cb_my_chans_page(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    session = data.get("my_chans_session")
    acc_id = data.get("my_chans_acc_id")
    if not session:
        await callback.message.edit_text("⚠️ Сессия устарела. Начните заново: /ops")
        return
    await _show_my_chans_page(
        callback.message, pool, session, acc_id, page=callback_data.page, edit=True,
        owner_id=callback.from_user.id,
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_refresh"))
async def cb_my_chans_refresh(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer("🔄 Обновляю из Telegram...")
    data = await state.get_data()
    session = data.get("my_chans_session")
    acc_id = callback_data.acc_id or data.get("my_chans_acc_id")
    if not session:
        await callback.message.edit_text("⚠️ Сессия устарела. Начните заново: /ops")
        return
    await _show_my_chans_page(
        callback.message, pool, session, acc_id, page=0, edit=True,
        force_refresh=True, owner_id=callback.from_user.id,
    )


async def _show_my_chans_page(
    msg, pool: asyncpg.Pool, session_str: str, acc_id: int, page: int,
    edit: bool = True, force_refresh: bool = False, owner_id: int = 0,
    acc_row: dict | None = None,
) -> None:
    from services import account_manager
    from database.db import get_managed_channels, upsert_managed_channels

    cached = await get_managed_channels(pool, owner_id, acc_id) if owner_id else []
    need_fetch = force_refresh or not cached

    if need_fetch:
        try:
            if edit:
                await msg.edit_text("⏳ Загружаю список каналов из Telegram...", parse_mode="HTML")
        except Exception:
            pass
        try:
            raw = await account_manager.get_dialogs(session_str, limit=200, _acc=acc_row)
        except Exception as e:
            kb = _back_kb()
            try:
                await msg.edit_text(
                    f"❌ Не удалось загрузить каналы: {html.escape(str(e)[:80])}",
                    parse_mode="HTML", reply_markup=kb.as_markup(),
                )
            except Exception:
                pass
            return
        if owner_id:
            await upsert_managed_channels(pool, owner_id, acc_id, raw)
        dialogs = raw
    else:
        dialogs = [
            {"id": r["channel_id"], "title": r["title"] or "", "username": r["username"] or "", "type": "channel", "members": 0}
            for r in cached
        ]

    total = len(dialogs)
    total_pages = max(1, (total + _CHANS_PAGE_SIZE - 1) // _CHANS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = dialogs[page * _CHANS_PAGE_SIZE:(page + 1) * _CHANS_PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for ch in chunk:
        ch_type = "📢" if ch.get("type") == "channel" else "👥"
        uname = f" @{ch['username']}" if ch.get("username") else ""
        members = f" · {ch['members']:,}" if ch.get("members") else ""
        label = f"{ch_type} {(ch.get('title') or '')[:28]}{uname}{members}"
        kb.button(text=label, callback_data=ChanCb(action="my_chans_item", channel_id=ch["id"], acc_id=acc_id))
    kb.adjust(1)

    nav_row = []
    if page > 0:
        nav_row.append(("◀ Пред.", ChanCb(action="my_chans_page", page=page - 1, acc_id=acc_id)))
    if page < total_pages - 1:
        nav_row.append(("След. ▶", ChanCb(action="my_chans_page", page=page + 1, acc_id=acc_id)))
    for btn_label, cd in nav_row:
        kb.button(text=btn_label, callback_data=cd)
    if nav_row:
        kb.adjust(*([1] * len(chunk)), len(nav_row))

    kb.button(text="🔄 Обновить из Telegram", callback_data=ChanCb(action="my_chans_refresh", acc_id=acc_id))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))

    src = "Telegram" if need_fetch else "кэш"
    text = (
        f"📋 <b>Мои каналы/чаты</b>\n\n"
        f"Всего: <b>{total}</b> · Страница <b>{page + 1}/{total_pages}</b> · <i>{src}</i>\n\n"
        "Нажмите на канал для управления:"
    )
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        pass


@router.callback_query(ChanCb.filter(F.action == "my_chans_item"))
async def cb_my_chans_item(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    ch_id = callback_data.channel_id
    acc_id = callback_data.acc_id
    data = await state.get_data()
    session = data.get("my_chans_session")

    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Опубликовать пост",  callback_data=ChanCb(action="my_chans_post",   channel_id=ch_id, acc_id=acc_id))
    kb.button(text="🚪 Покинуть",           callback_data=ChanCb(action="my_chans_leave",  channel_id=ch_id, acc_id=acc_id))
    kb.button(text="◀️ К списку",           callback_data=ChanCb(action="my_chans_page",   page=0, acc_id=acc_id))
    kb.adjust(1)

    await callback.message.edit_text(
        f"📋 <b>Действия с каналом</b>\n\n"
        f"ID: <code>{ch_id}</code>\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_leave"))
async def cb_my_chans_leave(
    callback: CallbackQuery, callback_data: ChanCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    session = data.get("my_chans_session")
    if not session:
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            callback_data.acc_id, callback.from_user.id,
        )
        session = acc["session_str"] if acc else None
    if not session:
        await callback.message.edit_text("⚠️ Сессия устарела. Начните заново: /ops")
        return
    from services import account_manager
    progress = await callback.message.edit_text("⏳ Покидаю канал...", parse_mode="HTML")
    ok = await account_manager.leave_channel(session, str(callback_data.channel_id), _acc=acc)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку", callback_data=ChanCb(action="my_chans_page", page=0, acc_id=callback_data.acc_id))
    await progress.edit_text(
        "✅ Вы покинули канал!" if ok else "❌ Не удалось покинуть канал. Проверьте права.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanCb.filter(F.action == "my_chans_post"))
async def cb_my_chans_post(
    callback: CallbackQuery, callback_data: ChanCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(
        my_chans_post_ch_id=callback_data.channel_id,
        my_chans_post_acc_id=callback_data.acc_id,
    )
    await state.set_state(MyChannelsFSM.posting)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="my_chans"))
    await callback.message.edit_text(
        f"📤 <b>Публикация поста</b>\n\n"
        f"Канал ID: <code>{callback_data.channel_id}</code>\n\n"
        "Введите текст поста (поддерживается HTML):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MyChannelsFSM.posting, F.text)
async def fsm_my_chans_post_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text_to_post = (message.text or "").strip()
    if not text_to_post:
        await message.answer("⚠️ Текст не может быть пустым.")
        return
    data = await state.get_data()
    ch_id = data.get("my_chans_post_ch_id")
    acc_id = data.get("my_chans_post_acc_id")
    await state.clear()

    session_row = await pool.fetchrow(
        "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, message.from_user.id,
    )
    if not session_row:
        await message.answer("⚠️ Аккаунт не найден. Начните заново: /ops")
        return
    from services import account_manager
    access_hash_row = await pool.fetchrow(
        "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
        message.from_user.id, ch_id,
    )
    access_hash = (access_hash_row["access_hash"] if access_hash_row else 0) or 0
    msg = await message.answer("⏳ Публикую...")
    result = await account_manager.post_to_channel(session_row["session_str"], ch_id, text_to_post, access_hash=access_hash, _acc=dict(session_row))
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К каналам", callback_data=ChanCb(action="my_chans"))
    if "msg_id" in result:
        await msg.edit_text(
            f"✅ <b>Пост опубликован!</b>\n\nID сообщения: <code>{result['msg_id']}</code>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    else:
        err_detail = html.escape(result.get("error", "неизвестная ошибка")[:120])
        await msg.edit_text(
            f"❌ <b>Ошибка публикации</b>\n\n<code>{err_detail}</code>",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )


# ── Contact Invite Flow ────────────────────────────────────────────────────────

def _cinv_channel_picker_kb(channels: list, page: int = 0) -> InlineKeyboardBuilder:
    PAGE = 10
    start = page * PAGE
    chunk = channels[start: start + PAGE]
    kb = InlineKeyboardBuilder()
    for ch in chunk:
        label = f"@{ch['username']}" if ch.get("username") else (ch.get("title") or str(ch["channel_id"]))[:32]
        kb.button(text=label, callback_data=ContactInvCb(action="pick_channel", channel_id=ch["channel_id"]))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardBuilder())
        kb.button(text="◀️", callback_data=ContactInvCb(action="chans_page", page=page - 1))
    if start + PAGE < len(channels):
        kb.button(text="▶️", callback_data=ContactInvCb(action="chans_page", page=page + 1))
    kb.button(text="✏️ Ввести вручную", callback_data=ContactInvCb(action="enter_channel"))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="menu"))
    kb.adjust(1)
    return kb


def _cinv_acc_picker_kb(accounts: list, selected: set) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        mark = "✅" if acc["id"] in selected else "⬜"
        label = _acc_label(acc)
        kb.button(text=f"{mark} {label}", callback_data=ContactInvCb(action="toggle_acc", acc_id=acc["id"]))
    if selected:
        kb.button(text=f"🚀 Продолжить ({len(selected)} акк.)", callback_data=ContactInvCb(action="proceed"))
    if len(selected) < len(accounts):
        kb.button(text="✅ Выбрать все", callback_data=ContactInvCb(action="all_accs"))
    else:
        kb.button(text="⬜ Снять выбор", callback_data=ContactInvCb(action="deselect_all"))
    kb.button(text="◀️ Назад", callback_data=ChanCb(action="contact_invite"))
    kb.adjust(1)
    return kb


@router.callback_query(ChanCb.filter(F.action == "contact_invite"))
async def cb_contact_invite_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.clear()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        from bot.utils.subscription import locked_text
        await callback.message.edit_text(
            locked_text("Инвайт из контактов", "pro"),
            parse_mode="HTML", reply_markup=_back_kb().as_markup(),
        )
        return
    channels = await _get_managed_channels_cached(pool, callback.from_user.id)
    kb = _cinv_channel_picker_kb(channels, 0)
    total = len(channels)
    text = (
        f"👥 <b>Инвайт из контактов</b>\n\n"
        f"Выберите канал/чат куда пригласить контакты со всех аккаунтов:\n\n"
        f"<i>Каналов в кэше: {total}. Нет нужного — введите вручную или обновите список в «Мои каналы/чаты».</i>"
        if channels else
        "👥 <b>Инвайт из контактов</b>\n\n"
        "Кэш каналов пуст. Введите @username или числовой ID канала/группы вручную.\n\n"
        "<i>Чтобы заполнить кэш: /ops → 📋 Мои каналы/чаты</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ContactInvCb.filter(F.action == "chans_page"))
async def cb_cinv_chans_page(
    callback: CallbackQuery, callback_data: ContactInvCb,
    pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    channels = await _get_managed_channels_cached(pool, callback.from_user.id)
    kb = _cinv_channel_picker_kb(channels, callback_data.page)
    await callback.message.edit_reply_markup(reply_markup=kb.as_markup())


@router.callback_query(ContactInvCb.filter(F.action == "enter_channel"))
async def cb_cinv_enter_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ContactInviteFSM.entering_channel)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanCb(action="contact_invite"))
    await callback.message.edit_text(
        "✏️ <b>Введите @username или ID канала/группы</b>\n\n"
        "Примеры:\n"
        "• <code>@mychannel</code>\n"
        "• <code>-1001234567890</code>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(ContactInviteFSM.entering_channel, F.text)
async def fsm_cinv_channel_input(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    raw = message.text.strip()
    try:
        ch_id = int(raw)
        identifier = ch_id
        display = str(ch_id)
        access_hash = 0
    except ValueError:
        identifier = raw if raw.startswith("@") else f"@{raw}"
        display = identifier
        access_hash = 0

    await state.update_data(channel_identifier=str(identifier), channel_display=display, channel_id=0, access_hash=access_hash)
    accounts = await _get_accounts(pool, message.from_user.id)
    if not accounts:
        await message.answer("⚠️ Нет подключённых аккаунтов. Добавьте через /accounts")
        await state.clear()
        return
    await state.set_state(ContactInviteFSM.choosing_accounts)
    kb = _cinv_acc_picker_kb(accounts, set())
    await message.answer(
        f"📱 <b>Выберите аккаунты</b>\n\nКанал: <b>{display}</b>\n\n"
        "Контакты будут собраны со всех выбранных аккаунтов и объединены по уникальным пользователям:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ContactInvCb.filter(F.action == "pick_channel"))
async def cb_cinv_pick_channel(
    callback: CallbackQuery, callback_data: ContactInvCb,
    pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    ch_id = callback_data.channel_id
    row = await pool.fetchrow(
        "SELECT title, username, access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
        callback.from_user.id, ch_id,
    )
    display = (f"@{row['username']}" if row and row["username"] else (row["title"] if row else str(ch_id)))
    access_hash = (row["access_hash"] if row else 0) or 0

    await state.update_data(
        channel_id=ch_id,
        channel_identifier=str(ch_id),
        channel_display=display,
        access_hash=access_hash,
    )
    accounts = await _get_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("⚠️ Нет подключённых аккаунтов. Добавьте через /accounts")
        await state.clear()
        return
    await state.set_state(ContactInviteFSM.choosing_accounts)
    kb = _cinv_acc_picker_kb(accounts, set())
    await callback.message.edit_text(
        f"📱 <b>Выберите аккаунты</b>\n\nКанал: <b>{display}</b>\n\n"
        "Контакты будут собраны со всех выбранных аккаунтов и объединены по уникальным пользователям:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


async def _cinv_refresh_acc_picker(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    data = await state.get_data()
    selected = set(data.get("selected_accs", []))
    display = data.get("channel_display", "?")
    accounts = await _get_accounts(pool, callback.from_user.id)
    kb = _cinv_acc_picker_kb(accounts, selected)
    await callback.message.edit_text(
        f"📱 <b>Выберите аккаунты</b>\n\nКанал: <b>{display}</b>\n\n"
        f"Выбрано: <b>{len(selected)}</b> из {len(accounts)}:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ContactInvCb.filter(F.action == "toggle_acc"))
async def cb_cinv_toggle_acc(
    callback: CallbackQuery, callback_data: ContactInvCb,
    pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected = set(data.get("selected_accs", []))
    if callback_data.acc_id in selected:
        selected.discard(callback_data.acc_id)
    else:
        selected.add(callback_data.acc_id)
    await state.update_data(selected_accs=list(selected))
    await _cinv_refresh_acc_picker(callback, pool, state)


@router.callback_query(ContactInvCb.filter(F.action == "all_accs"))
async def cb_cinv_all_accs(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    accounts = await _get_accounts(pool, callback.from_user.id)
    selected = {a["id"] for a in accounts}
    await state.update_data(selected_accs=list(selected))
    await _cinv_refresh_acc_picker(callback, pool, state)


@router.callback_query(ContactInvCb.filter(F.action == "deselect_all"))
async def cb_cinv_deselect_all(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(selected_accs=[])
    await _cinv_refresh_acc_picker(callback, pool, state)


@router.callback_query(ContactInvCb.filter(F.action == "proceed"))
async def cb_cinv_proceed(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    data = await state.get_data()
    selected_accs = data.get("selected_accs", [])
    channel_display = data.get("channel_display", "?")
    if not selected_accs:
        await callback.answer("Выберите хотя бы один аккаунт.", show_alert=True)
        return
    await callback.answer()
    msg = await callback.message.edit_text(
        f"⏳ Подсчёт контактов с {len(selected_accs)} аккаунт(ов)...",
        parse_mode="HTML",
    )
    from services import account_manager as _am
    acc_rows = await pool.fetch(
        "SELECT id, session_str, first_name, username, device_model, system_version, app_version FROM tg_accounts "
        "WHERE id = ANY($1::int[]) AND owner_id=$2 AND is_active=true",
        selected_accs, callback.from_user.id,
    )
    unique_ids: set[int] = set()
    for acc in acc_rows:
        contacts = await _am.get_contacts(acc["session_str"], _acc=dict(acc))
        for c in contacts:
            unique_ids.add(c["user_id"])

    await state.update_data(contact_count=len(unique_ids))
    await state.set_state(ContactInviteFSM.confirming)
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить инвайт", callback_data=ContactInvCb(action="run"))
    kb.button(text="❌ Отмена", callback_data=ContactInvCb(action="cancel"))
    kb.adjust(1)
    est_min = max(1, len(unique_ids) // 60)
    await msg.edit_text(
        f"👥 <b>Подтверждение инвайта</b>\n\n"
        f"Канал: <b>{channel_display}</b>\n"
        f"Аккаунтов: <b>{len(acc_rows)}</b>\n"
        f"Уникальных контактов: <b>{len(unique_ids):,}</b>\n\n"
        f"⏱ Примерное время: ~{est_min} мин.\n"
        f"Процесс запустится в фоне, уведомление придёт по завершении.",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(ContactInvCb.filter(F.action == "run"))
async def cb_cinv_run(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    data = await state.get_data()
    selected_accs = data.get("selected_accs", [])
    channel_id = data.get("channel_id", 0)
    channel_identifier = data.get("channel_identifier", "")
    channel_display = data.get("channel_display", "?")
    access_hash = data.get("access_hash", 0)
    if not selected_accs or not channel_identifier:
        await callback.answer("Недостаточно данных. Начните заново.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    acc_rows = await pool.fetch(
        "SELECT a.id, a.session_str, a.first_name, a.username, a.phone, "
        "a.device_model, a.system_version, a.app_version, p.proxy_url "
        "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.id = ANY($1::int[]) AND a.owner_id=$2 AND a.is_active=true",
        selected_accs, callback.from_user.id,
    )
    if not acc_rows:
        await callback.message.edit_text("⚠️ Аккаунты не найдены или деактивированы.")
        return
    await callback.message.edit_text(
        f"🚀 <b>Инвайт запущен в фоне</b>\n\n"
        f"Канал: <b>{channel_display}</b>\n"
        f"Аккаунтов: <b>{len(acc_rows)}</b>\n\n"
        "⚠️ <i>Все участвующие аккаунты должны быть администраторами канала.\n"
        "Используйте: Управление каналом → 👑 Со-Администраторы</i>\n\n"
        "Уведомление придёт когда всё завершится.",
        parse_mode="HTML", reply_markup=_back_kb().as_markup(),
    )
    asyncio.create_task(_cinv_bg(
        bot=callback.bot,
        user_id=callback.from_user.id,
        acc_rows=list(acc_rows),
        channel_id=channel_id,
        channel_identifier=channel_identifier,
        access_hash=access_hash,
        channel_display=channel_display,
        pool=pool,
    ))


@router.callback_query(ContactInvCb.filter(F.action == "cancel"))
async def cb_cinv_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено.")
    await callback.message.edit_text("❌ Инвайт отменён.", reply_markup=_back_kb().as_markup())


async def _get_managed_channels_cached(pool: asyncpg.Pool, owner_id: int) -> list:
    from database import db as _db
    return await _db.get_managed_channels(pool, owner_id)


async def _cinv_bg(
    bot, user_id: int, acc_rows: list,
    channel_id: int, channel_identifier: str, access_hash: int, channel_display: str,
    pool=None,
) -> None:
    """Background task: collect contacts from accounts and invite to channel.

    The first account in acc_rows is treated as the primary (channel admin).
    It auto-promotes all other accounts to admin before distributing invite work.
    """
    from services import account_manager as _am

    chan_target = channel_id if channel_id else channel_identifier

    # 0. Auto-add co-accounts to channel, then promote to admin
    # IMPORTANT: Accounts must be members before they can be promoted to admin
    if len(acc_rows) > 1 and pool is not None:
        primary = acc_rows[0]
        primary_dict = dict(primary)

        # First, ensure all co-accounts are members by having them join the channel
        # Use the primary account's username (@username) or generate invite link
        join_identifier = None
        if channel_identifier and channel_identifier.startswith("@"):
            join_identifier = channel_identifier
        else:
            # Try to get invite link from primary account
            try:
                invite_link = await _am.get_channel_invite_link(
                    primary["session_str"], chan_target, _acc=primary_dict
                )
                if invite_link:
                    join_identifier = invite_link
            except Exception as e:
                log.warning("cinv get_invite_link: %s", e)

        # Add each co-account to the channel
        added_ok = 0
        for other in acc_rows[1:]:
            if join_identifier:
                try:
                    result = await _am.join_channel(
                        other["session_str"], join_identifier, _acc=dict(other)
                    )
                    if not result.get("error"):
                        added_ok += 1
                        log.info("cinv: co-account %s joined %s", other["id"], chan_target)
                except Exception as e:
                    log.warning("cinv co-account join: %s", e)
            await asyncio.sleep(1)

        if added_ok > 0:
            log.info("cinv: added %d co-accounts as members in %s", added_ok, chan_target)

        # Now promote co-accounts to admin (they are members now)
        promo_ok = 0
        for other in acc_rows[1:]:
            tg_uid = other.get("tg_user_id")
            if not tg_uid:
                try:
                    row = await pool.fetchrow(
                        "SELECT tg_user_id FROM tg_accounts WHERE id=$1", other["id"]
                    )
                    tg_uid = row["tg_user_id"] if row else None
                except Exception:
                    pass
            if tg_uid:
                try:
                    ok = await _am.promote_to_admin(
                        primary["session_str"], chan_target, tg_uid, _acc=primary_dict
                    )
                    if ok:
                        promo_ok += 1
                        log.info("cinv: promoted co-account %s to admin", tg_uid)
                except Exception as e:
                    log.warning("cinv auto-promote acc=%s: %s", other["id"], e)
            await asyncio.sleep(2)

        if promo_ok > 0:
            log.info("cinv: promoted %d co-accounts to admin in %s", promo_ok, chan_target)

    # 1. Collect and deduplicate contacts
    contacts_map: dict[int, dict] = {}
    for acc in acc_rows:
        try:
            for c in await _am.get_contacts(acc["session_str"], _acc=dict(acc)):
                contacts_map[c["user_id"]] = c
        except Exception as e:
            log.warning("cinv get_contacts acc=%s: %s", acc["id"], e)

    if not contacts_map:
        try:
            await bot.send_message(
                user_id,
                "⚠️ <b>Инвайт: нет контактов</b>\n\nНи у одного аккаунта не найдено контактов.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # 2. Build identifier list: @username preferred, phone as fallback
    identifiers: list[str] = []
    for c in contacts_map.values():
        if c["username"]:
            identifiers.append(f"@{c['username']}")
        elif c["phone"]:
            ph = c["phone"]
            identifiers.append(ph if ph.startswith("+") else f"+{ph}")

    if not identifiers:
        try:
            await bot.send_message(
                user_id,
                "⚠️ <b>Инвайт: нет идентификаторов</b>\n\n"
                "У контактов нет username и телефонов — невозможно пригласить.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # 3. Split list among accounts (round-robin distribution)
    n = len(acc_rows)
    chunks = [identifiers[i::n] for i in range(n)]

    total_invited = 0
    total_failed = 0
    for acc, chunk in zip(acc_rows, chunks):
        if not chunk:
            continue
        try:
            res = await _am.invite_users_to_channel(acc["session_str"], chan_target, chunk, _acc=dict(acc))
            total_invited += res.get("invited", 0)
            total_failed += len(res.get("failed", []))
        except Exception as e:
            log.warning("cinv invite acc=%s: %s", acc["id"], e)
            total_failed += len(chunk)

    # 4. Notify user
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Инвайт завершён!</b>\n\n"
            f"Канал: <b>{html.escape(channel_display)}</b>\n"
            f"Всего контактов: <b>{len(identifiers):,}</b>\n"
            f"Приглашено: <b>{total_invited}</b>\n"
            f"Не удалось: <b>{total_failed}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
