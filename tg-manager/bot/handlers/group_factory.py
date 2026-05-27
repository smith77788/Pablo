"""Group Factory handler.

Provides group management via Telethon accounts:
  - Create supergroups / regular groups
  - List groups/supergroups of an account
  - Send announcements to all groups of an account
"""
from __future__ import annotations

import asyncio
import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import GroupFCb
from bot.states import AnnounceGroupFSM, CreateGroupFSM

log = logging.getLogger(__name__)
router = Router()


# ── Helpers ────────────────────────────────────────────────────────────────

async def _get_active_accounts(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT id, phone, first_name, username, is_active FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE ORDER BY added_at",
        owner_id,
    )


def _acc_label(acc: asyncpg.Record) -> str:
    name = acc["first_name"] or ""
    uname = f"@{acc['username']}" if acc["username"] else acc["phone"]
    return f"{name} ({uname})" if name else uname


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    return kb


# ── Menu ───────────────────────────────────────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "menu"))
async def cb_group_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать группу",  callback_data=GroupFCb(action="create"))
    kb.button(text="📋 Мои группы",      callback_data=GroupFCb(action="list"))
    kb.button(text="👥 Участники",       callback_data=GroupFCb(action="members"))
    kb.button(text="📢 Объявление",      callback_data=GroupFCb(action="announce"))
    kb.button(text="◀️ Назад",          callback_data="main_menu")
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        "👥 <b>Менеджер групп</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create group — Step 1: choose account ─────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "create"))
async def cb_group_create_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\nПодключите аккаунт через /accounts",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=f"✅ {_acc_label(acc)}",
            callback_data=GroupFCb(action="create_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    kb.adjust(1)

    await state.set_state(CreateGroupFSM.choosing_account)
    await callback.message.edit_text(
        "➕ <b>Создание группы</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create group — Step 2: account chosen → ask title ─────────────────────

@router.callback_query(GroupFCb.filter(F.action == "create_acc"))
async def cb_group_create_acc_chosen(
    callback: CallbackQuery,
    callback_data: GroupFCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await pool.fetchrow(
        "SELECT id, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id, callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
    await state.set_state(CreateGroupFSM.waiting_title)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
    await callback.message.edit_text(
        "📝 <b>Название группы</b>\n\nВведите название (до 128 символов):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create group — Step 3: title entered → ask about ──────────────────────

@router.message(CreateGroupFSM.waiting_title)
async def fsm_group_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer("⚠️ Название от 1 до 128 символов. Попробуйте ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(CreateGroupFSM.waiting_about)

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=GroupFCb(action="skip_about"))
    kb.button(text="❌ Отмена",     callback_data=GroupFCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "📄 <b>Описание группы</b>\n\nВведите описание или пропустите:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GroupFCb.filter(F.action == "skip_about"))
async def cb_group_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await _show_type_choice(callback.message, state, edit=True)


@router.message(CreateGroupFSM.waiting_about)
async def fsm_group_about(message: Message, state: FSMContext) -> None:
    about = (message.text or "").strip()[:255]
    await state.update_data(about=about)
    await _show_type_choice(message, state, edit=False)


# ── Create group — Step 4: choose type ────────────────────────────────────

async def _show_type_choice(msg, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(CreateGroupFSM.choosing_type)
    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 Супергруппа",      callback_data=GroupFCb(action="type_super"))
    kb.button(text="👥 Обычная группа",   callback_data=GroupFCb(action="type_basic"))
    kb.button(text="❌ Отмена",           callback_data=GroupFCb(action="menu"))
    kb.adjust(2, 1)
    text = (
        "🔧 <b>Тип группы</b>\n\n"
        "• <b>Супергруппа</b> — неограниченное количество участников, история сообщений\n"
        "• <b>Обычная группа</b> — до 200 участников"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GroupFCb.filter(F.action.in_({"type_super", "type_basic"})))
async def cb_group_type_chosen(
    callback: CallbackQuery, callback_data: GroupFCb, state: FSMContext
) -> None:
    await callback.answer()
    is_super = callback_data.action == "type_super"
    await state.update_data(is_super=is_super)
    await _show_group_confirm(callback.message, state, edit=True)


# ── Create group — Step 5: confirm ────────────────────────────────────────

async def _show_group_confirm(msg, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(CreateGroupFSM.confirming)
    data = await state.get_data()
    title = html.escape(data.get("title", ""))
    about = html.escape(data.get("about", ""))
    is_super = data.get("is_super", True)
    group_type = "Супергруппа" if is_super else "Обычная группа"

    text = (
        f"✅ <b>Подтвердите создание группы</b>\n\n"
        f"Тип: <b>{group_type}</b>\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: <b>{about or '—'}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать",  callback_data=GroupFCb(action="do_create"))
    kb.button(text="❌ Отмена",  callback_data=GroupFCb(action="menu"))
    kb.adjust(2)

    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Create group — Step 6: do create ──────────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "do_create"))
async def cb_group_do_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю группу...")
    data = await state.get_data()
    await state.clear()

    acc_id = data.get("acc_id")
    if not acc_id:
        await callback.message.edit_text(
            "⚠️ Сессия истекла. Начните заново.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    acc = await pool.fetchrow(
        "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, callback.from_user.id,
    )
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    from services import account_manager

    # Use create_channel with megagroup flag (works for supergroups)
    # For basic groups, fall back to create_channel as well (megagroup=False also works)
    is_super = data.get("is_super", True)
    try:
        result = await account_manager.create_channel(
            acc["session_str"],
            title=data["title"],
            about=data.get("about", ""),
            megagroup=is_super,
            _acc=acc,
        )
    except Exception as e:
        result = {"error": str(e)}

    if "error" in result:
        err = html.escape(str(result["error"]))
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания группы</b>\n\n<code>{err}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    title_s = html.escape(result.get("title", data["title"]))
    group_id = result.get("channel_id", 0)
    invite = result.get("invite_link", "")
    group_type = "Супергруппа" if is_super else "Группа"

    await callback.message.edit_text(
        f"✅ <b>{group_type} создана!</b>\n\n"
        f"Название: <b>{title_s}</b>\n"
        f"ID: <code>{group_id}</code>\n"
        + (f"Ссылка: {html.escape(invite)}" if invite else ""),
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


# ── List groups ────────────────────────────────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "list"))
async def cb_group_list_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=GroupFCb(action="list_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📋 <b>Мои группы</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GroupFCb.filter(F.action == "list_acc"))
async def cb_group_list_acc(
    callback: CallbackQuery, callback_data: GroupFCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю группы...")
    acc = await pool.fetchrow(
        "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id, callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    groups = [
        d for d in (dialogs or [])
        if d.get("type") in ("megagroup", "supergroup", "group", "chat")
    ]

    if not groups:
        await callback.message.edit_text(
            "📋 У этого аккаунта нет групп.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    lines = ["📋 <b>Мои группы</b>\n"]
    for g in groups[:20]:
        icon = "🌐" if g.get("type") in ("megagroup", "supergroup") else "👥"
        title = html.escape(g.get("title", f"id={g['id']}"))
        lines.append(f"{icon} {title} — <code>{g['id']}</code>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


# ── Members stub ───────────────────────────────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "members"))
async def cb_group_members(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "👥 <b>Управление участниками</b>\n\n🚧 В разработке",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


# ── Announce — Step 1: choose account ─────────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "announce"))
async def cb_group_announce_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=GroupFCb(action="announce_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📢 <b>Объявление во все группы</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Announce — Step 2: account chosen → ask text ──────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "announce_acc"))
async def cb_group_announce_acc(
    callback: CallbackQuery, callback_data: GroupFCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    acc = await pool.fetchrow(
        "SELECT id, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id, callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
    await state.set_state(AnnounceGroupFSM.waiting_text)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
    await callback.message.edit_text(
        "📢 <b>Текст объявления</b>\n\nВведите текст для рассылки во все группы аккаунта:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Announce — Step 3: text entered → confirm ─────────────────────────────

@router.message(AnnounceGroupFSM.waiting_text)
async def fsm_announce_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Введите текст объявления:")
        return
    await state.update_data(announce_text=text)
    await state.set_state(AnnounceGroupFSM.confirming)

    preview = html.escape(text[:200])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data=GroupFCb(action="do_announce"))
    kb.button(text="❌ Отмена",   callback_data=GroupFCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        f"📢 <b>Подтвердите объявление</b>\n\n"
        f"Текст:\n<i>{preview}</i>\n\n"
        "Будет отправлено во все группы выбранного аккаунта.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Announce — Step 4: do send ─────────────────────────────────────────────

@router.callback_query(GroupFCb.filter(F.action == "do_announce"))
async def cb_group_do_announce(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Отправляю...")
    data = await state.get_data()
    await state.clear()

    acc_id = data.get("acc_id")
    announce_text = data.get("announce_text", "")
    if not acc_id or not announce_text:
        await callback.message.edit_text(
            "⚠️ Сессия истекла. Начните заново.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    acc = await pool.fetchrow(
        "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, callback.from_user.id,
    )
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    from services import account_manager
    dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    groups = [
        d for d in (dialogs or [])
        if d.get("type") in ("megagroup", "supergroup", "group", "chat")
    ]

    if not groups:
        await callback.message.edit_text(
            "📋 У этого аккаунта нет групп.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    total = len(groups)
    ok_count = 0
    err_count = 0
    progress_msg = await callback.message.edit_text(
        f"⏳ Отправляю объявление... 0/{total}",
        parse_mode="HTML",
    )

    for idx, grp in enumerate(groups, 1):
        access_hash = grp.get("access_hash", 0) or 0
        try:
            result = await account_manager.post_to_channel(
                acc["session_str"],
                grp["id"],
                announce_text,
                access_hash=access_hash,
                _acc=acc,
            )
            if "error" in result or result.get("banned"):
                err_count += 1
            else:
                ok_count += 1
        except Exception:
            err_count += 1

        try:
            await progress_msg.edit_text(
                f"⏳ Отправляю объявление... {idx}/{total}\n✅ {ok_count} ❌ {err_count}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await asyncio.sleep(3)

    await progress_msg.edit_text(
        f"✅ <b>Объявление отправлено</b>\n\n"
        f"Всего групп: {total}\n"
        f"Успешно: {ok_count}\n"
        f"Ошибок: {err_count}",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )
