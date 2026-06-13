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

from bot.callbacks import AccCb, BmCb, GroupFCb, EcoPickCb
from database import db
from bot.keyboards import subscription_locked_markup
from bot.states import AnnounceGroupFSM, CreateGroupFSM
from bot.utils.op_helpers import _acc_label, _get_active_accounts
from bot.utils.subscription import locked_text, require_plan
from services import task_registry as _treg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    return kb


def _no_accounts_kb() -> InlineKeyboardBuilder:
    """Клавиатура для экранов 'нет активных аккаунтов'."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Перейти к аккаунтам", callback_data=AccCb(action="menu"))
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    kb.adjust(1)
    return kb


# ── Menu ───────────────────────────────────────────────────────────────────


@router.callback_query(GroupFCb.filter(F.action == "menu"))
async def cb_group_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать группу", callback_data=GroupFCb(action="create"))
    kb.button(text="📥 Импорт из Telegram", callback_data=GroupFCb(action="import"))
    kb.button(text="📋 Мои группы", callback_data=GroupFCb(action="list"))
    kb.button(text="👥 Участники", callback_data=GroupFCb(action="members"))
    kb.button(text="📢 Объявление", callback_data=GroupFCb(action="announce"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 2, 1)
    await callback.message.edit_text(
        "👥 <b>Менеджер групп</b>\n\n"
        "• <b>Создать группу</b> — новая группа через ваш аккаунт\n"
        "• <b>Импорт из Telegram</b> — подключить существующие группы\n"
        "• <b>Мои группы</b> — список всех групп аккаунтов\n"
        "• <b>Участники</b> — просмотр участников группы\n"
        "• <b>Объявление</b> — отправить сообщение во все группы",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create group — Step 1: choose account ─────────────────────────────────


@router.callback_query(GroupFCb.filter(F.action == "create"))
async def cb_group_create_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Создание групп", "pro"),
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для создания группы нужен хотя бы один активный Telegram-аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
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
    try:
        acc = await db.get_account_for_telethon(
            pool, callback_data.acc_id, callback.from_user.id
        )
    except Exception:
        log_exc_swallow(log, "group_create_acc fetchrow failed")
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])

    sd = await state.get_data()
    prefill = sd.get("tpl_prefill") or {}
    if prefill.get("title"):
        await state.update_data(
            title=prefill.get("title", ""),
            about=prefill.get("description") or prefill.get("about") or "",
            is_super=True,
            tpl_prefill=None,
        )
        await _show_group_confirm(callback.message, state, edit=True)
        return

    await state.set_state(CreateGroupFSM.waiting_title)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
    await callback.message.edit_text(
        "📝 <b>Название группы</b>\n\nВведите название (до 128 символов):\n\n"
        "💡 <b>Примеры:</b>\n"
        "• <code>Python Developers | Chat</code>\n"
        "• <code>Клуб Инвесторов</code>\n"
        "• <code>Gaming Community 🎮</code>",
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
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
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
    kb.button(text="🌐 Супергруппа", callback_data=GroupFCb(action="type_super"))
    kb.button(text="👥 Обычная группа", callback_data=GroupFCb(action="type_basic"))
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
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
            log_exc_swallow(log, "Ошибка редактирования сообщения выбора типа группы")
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
    kb.button(text="✅ Создать", callback_data=GroupFCb(action="do_create"))
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
    kb.adjust(2)

    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Ошибка редактирования сообщения подтверждения создания группы"
            )
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

    try:
        acc = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            acc_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "group_confirm_create fetchrow failed")
        acc = None
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

    kb_grp = InlineKeyboardBuilder()
    kb_grp.button(
        text="🌐 Добавить в экосистему",
        callback_data=EcoPickCb(action="list", object_type="group", object_id=group_id),
    )
    kb_grp.button(text="◀️ Меню", callback_data=GroupFCb(action="menu"))
    kb_grp.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>{group_type} создана!</b>\n\n"
        f"Название: <b>{title_s}</b>\n"
        f"ID: <code>{group_id}</code>\n"
        + (f"Ссылка: {html.escape(invite)}" if invite else ""),
        parse_mode="HTML",
        reply_markup=kb_grp.as_markup(),
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
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты, затем вернитесь сюда.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
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
    try:
        acc = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            callback_data.acc_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "group_list_acc fetchrow failed")
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю группы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    except Exception as _e:
        log.warning("my_groups get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список групп: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    groups = [
        d
        for d in (dialogs or [])
        if d.get("type") in ("megagroup", "supergroup", "group", "chat")
    ]

    if not groups:
        await callback.message.edit_text(
            "📋 <b>Мои группы</b>\n\n"
            "⚠️ У этого аккаунта нет групп в Telegram.\n\n"
            "💡 Создайте первую группу через <b>➕ Создать группу</b> "
            "или подключите существующие через <b>📥 Импорт из Telegram</b>.",
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


# ── Members — Step 1: choose account ─────────────────────────────────────


@router.callback_query(GroupFCb.filter(F.action == "members"))
async def cb_group_members(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты, затем вернитесь сюда.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=GroupFCb(action="members_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👥 <b>Участники групп</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Members — Step 2: choose group ────────────────────────────────────────


@router.callback_query(GroupFCb.filter(F.action == "members_acc"))
async def cb_group_members_acc(
    callback: CallbackQuery, callback_data: GroupFCb, pool: asyncpg.Pool
) -> None:
    try:
        acc = await db.get_account_for_telethon(
            pool, callback_data.acc_id, callback.from_user.id
        )
    except Exception:
        log_exc_swallow(log, "group_members_acc fetchrow failed")
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю группы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    except Exception as _e:
        log.warning("members_acc get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список групп: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    groups = [
        d
        for d in (dialogs or [])
        if d.get("type") in ("megagroup", "supergroup", "group", "chat")
    ]

    if not groups:
        await callback.message.edit_text(
            "👥 <b>Участники групп</b>\n\n"
            "⚠️ У этого аккаунта нет групп в Telegram.\n\n"
            "💡 Создайте первую группу через <b>➕ Создать группу</b> "
            "или подключите существующие через <b>📥 Импорт из Telegram</b>.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for g in groups[:20]:
        icon = "🌐" if g.get("type") in ("megagroup", "supergroup") else "👥"
        title = html.escape(g.get("title", f"id={g['id']}"))
        kb.button(
            text=f"{icon} {title}",
            callback_data=GroupFCb(
                action="members_list", acc_id=acc["id"], group_id=g["id"]
            ),
        )
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="members"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👥 <b>Участники групп</b>\n\nВыберите группу:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Members — Step 3: show members ────────────────────────────────────────


@router.callback_query(GroupFCb.filter(F.action == "members_list"))
async def cb_group_members_list(
    callback: CallbackQuery, callback_data: GroupFCb, pool: asyncpg.Pool
) -> None:
    try:
        acc = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            callback_data.acc_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "group_members_list fetchrow failed")
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю участников...")
    from services import account_manager

    members = await account_manager.get_channel_members(
        acc["session_str"], callback_data.group_id, limit=50, _acc=acc
    )

    if not members:
        await callback.message.edit_text(
            "👥 Участников не найдено или нет прав для просмотра.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    lines = [f"👥 <b>Участники</b> ({len(members)} чел.)\n"]
    for m in members[:30]:
        if m.get("is_bot"):
            icon = "🤖"
        else:
            icon = "👤"
        name = html.escape((m.get("first_name") or "").strip() or f"id{m['user_id']}")
        uname = f" @{html.escape(m['username'])}" if m.get("username") else ""
        lines.append(f"{icon} {name}{uname}")
    if len(members) > 30:
        lines.append(f"\n<i>... и ещё {len(members) - 30} участников</i>")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад",
        callback_data=GroupFCb(action="members_acc", acc_id=callback_data.acc_id),
    )
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# IMPORT EXISTING GROUPS — подключить уже существующие группы
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(GroupFCb.filter(F.action == "import"))
async def cb_group_import(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Step 1: выбор аккаунта для импорта групп."""
    await callback.answer()
    from bot.utils.op_helpers import _get_active_accounts, _acc_label

    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для импорта групп нужен хотя бы один активный Telegram-аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=GroupFCb(action="import_acc", acc_id=acc["id"]),
        )
    kb.button(text="🔄 Все аккаунты сразу", callback_data=GroupFCb(action="import_all"))
    kb.button(text="◀️ Назад", callback_data=GroupFCb(action="menu"))
    kb.adjust(2, 1, 1)
    await callback.message.edit_text(
        "📥 <b>Импорт существующих групп</b>\n\n"
        "Мы загрузим список групп/супергрупп из выбранного аккаунта "
        "и подключим их к системе.\n\n"
        "Выберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GroupFCb.filter(F.action == "import_acc"))
async def cb_group_import_acc(
    callback: CallbackQuery, callback_data: GroupFCb, pool: asyncpg.Pool
) -> None:
    """Загрузить группы аккаунта и сохранить в managed_channels."""
    try:
        acc = await pool.fetchrow(
            "SELECT id, session_str, phone, first_name, username, "
            "device_model, system_version, app_version FROM tg_accounts "
            "WHERE id=$1 AND owner_id=$2",
            callback_data.acc_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "group_import_acc fetchrow failed")
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю группы из Telegram...")

    from services import account_manager
    from database.db import upsert_managed_channels

    try:
        dialogs = (
            await account_manager.get_dialogs(acc["session_str"], limit=200, _acc=acc)
            or []
        )
    except Exception as e:
        log.warning("group import_acc get_dialogs error: %s", e)
        await callback.message.edit_text(
            f"❌ Ошибка при получении диалогов: <code>{html.escape(str(e)[:100])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    groups = [
        d
        for d in dialogs
        if d.get("type") in ("megagroup", "supergroup", "group", "chat", "gigagroup")
    ]
    if not groups:
        await callback.message.edit_text(
            "📥 <b>Импорт групп</b>\n\n"
            "ℹ️ У этого аккаунта нет групп в Telegram.\n\n"
            "💡 Чтобы появились группы — создайте новую через <b>➕ Создать группу</b> "
            "или вступите в существующие группы через этот аккаунт в Telegram.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    await upsert_managed_channels(pool, callback.from_user.id, acc["id"], groups)

    lines = [f"📥 <b>Импортировано групп: {len(groups)}</b>\n"]
    lines += [
        f"• {html.escape(g.get('title', '(без названия)'))}"
        + (" 🌐" if g.get("type") in ("megagroup", "supergroup") else "")
        for g in groups[:20]
    ]
    if len(groups) > 20:
        lines.append(f"... и ещё {len(groups) - 20}")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню групп", callback_data=GroupFCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _group_import_all_bg(
    pool: asyncpg.Pool,
    owner_id: int,
    progress_msg,
    accounts: list,
) -> None:
    from services import account_manager
    from database.db import upsert_managed_channels

    total = 0
    errors = []
    n = len(accounts)
    try:
        for idx, acc in enumerate(accounts):
            try:
                dialogs = (
                    await account_manager.get_dialogs(
                        acc["session_str"], limit=200, _acc=acc
                    )
                    or []
                )
                groups = [
                    d
                    for d in dialogs
                    if d.get("type")
                    in ("megagroup", "supergroup", "group", "chat", "gigagroup")
                ]
                if groups:
                    await upsert_managed_channels(pool, owner_id, acc["id"], groups)
                    total += len(groups)
                try:
                    await progress_msg.edit_text(
                        f"⏳ Обработка {idx + 1}/{n} аккаунтов...\nНайдено групп: {total}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                if idx < n - 1:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("_group_import_all_bg acc=%s error: %s", acc.get("id"), e)
                errors.append(f"• {_acc_label(acc)}: {str(e)[:50]}")
    except asyncio.CancelledError:
        log.info("_group_import_all_bg: отменено")
        raise
    except Exception:
        log_exc_swallow(log, "_group_import_all_bg: неожиданная ошибка")

    text = f"✅ <b>Импорт завершён</b>\n\nПодключено групп: <b>{total}</b>"
    if errors:
        text += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню групп", callback_data=GroupFCb(action="menu"))
    try:
        await progress_msg.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception:
        log_exc_swallow(log, "_group_import_all_bg: сбой финального отчёта")


@router.callback_query(GroupFCb.filter(F.action == "import_all"))
async def cb_group_import_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Импортировать группы со всех активных аккаунтов."""
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.answer("Нет активных аккаунтов.", show_alert=True)
        return
    await callback.answer()

    from services import operation_bus

    op_id = await operation_bus.submit(
        pool, callback.from_user.id, "group_import_all",
        {"account_ids": [int(a["id"]) for a in accounts]},
        total_items=len(accounts),
    )
    await callback.message.edit_text(
        f"✅ <b>Импорт групп запущен</b>\n\n"
        f"Аккаунтов: <b>{len(accounts)}</b>\n"
        f"📋 Операция <code>#{op_id}</code> в очереди\n"
        f"💡 Статус: /ops",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder().button(
            text="◀️ В меню групп", callback_data=GroupFCb(action="menu")
        ).as_markup(),
    )


# ── Announce — Step 1: choose account ─────────────────────────────────────


@router.callback_query(GroupFCb.filter(F.action == "announce"))
async def cb_group_announce_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Объявление во все группы", "starter"),
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты, затем вернитесь сюда.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
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
    callback: CallbackQuery,
    callback_data: GroupFCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    try:
        acc = await db.get_account_for_telethon(
            pool, callback_data.acc_id, callback.from_user.id
        )
    except Exception:
        log_exc_swallow(log, "group_announce_acc fetchrow failed")
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], session_str=acc["session_str"])
    await state.set_state(AnnounceGroupFSM.waiting_text)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
    await callback.message.edit_text(
        "📢 <b>Текст объявления</b>\n\nВведите текст для рассылки во все группы аккаунта:\n\n"
        "💡 Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code>",
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
    kb.button(text="❌ Отмена", callback_data=GroupFCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        f"📢 <b>Подтвердите объявление</b>\n\n"
        f"Текст:\n<i>{preview}</i>\n\n"
        "Будет отправлено во все группы выбранного аккаунта.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Announce — Step 4: do send ─────────────────────────────────────────────


async def _group_announce_bg(
    acc: dict, groups: list, announce_text: str, progress_msg, user_id: int
) -> None:
    """Фоновая отправка объявления во все группы аккаунта."""
    from services import account_manager

    total = len(groups)
    ok_count = 0
    err_count = 0
    try:
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
            except asyncio.CancelledError:
                raise
            except Exception:
                log_exc_swallow(
                    log, "Ошибка отправки объявления в группу %s", grp.get("id")
                )
                err_count += 1

            try:
                await progress_msg.edit_text(
                    f"⏳ Отправляю объявление... {idx}/{total}\n✅ {ok_count} ❌ {err_count}",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Ошибка обновления прогресса отправки объявления")
            if idx < total:
                await asyncio.sleep(3)
    except asyncio.CancelledError:
        try:
            await progress_msg.edit_text(
                f"❌ <b>Объявление отменено</b>\n\n✅ Отправлено: <b>{ok_count}</b>  ❌ Ошибок: <b>{err_count}</b>",
                parse_mode="HTML",
                reply_markup=_back_menu_kb().as_markup(),
            )
        except Exception:
            pass
        raise
    except Exception as exc:
        log.exception("group_announce_bg FATAL user=%s: %s", user_id, exc)
        try:
            await progress_msg.edit_text(
                f"❌ <b>Ошибка при отправке объявления</b>\n\n<code>{html.escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=_back_menu_kb().as_markup(),
            )
        except Exception:
            pass
        return

    try:
        await progress_msg.edit_text(
            f"✅ <b>Объявление отправлено</b>\n\n"
            f"Всего групп: {total}\n"
            f"Успешно: {ok_count}\n"
            f"Ошибок: {err_count}",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
    except Exception:
        pass


@router.callback_query(GroupFCb.filter(F.action == "do_announce"))
async def cb_group_do_announce(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю...")
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

    try:
        acc = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            acc_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "group_do_announce fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
    except Exception as exc:
        log.warning("group_do_announce: get_dialogs failed: %s", exc)
        await callback.message.edit_text(
            f"❌ Не удалось получить список групп: <code>{html.escape(str(exc)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    groups = [
        d
        for d in (dialogs or [])
        if d.get("type") in ("megagroup", "supergroup", "group", "chat")
    ]

    if not groups:
        await callback.message.edit_text(
            "📋 У этого аккаунта нет групп.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    from services import operation_bus

    op_id = await operation_bus.submit(
        pool, callback.from_user.id, "group_announce",
        {"acc_id": acc_id, "text": announce_text},
        total_items=len(groups),
    )
    await callback.message.edit_text(
        f"✅ <b>Объявление поставлено в очередь</b>\n\n"
        f"Групп: <b>{len(groups)}</b>\n"
        f"📋 Операция <code>#{op_id}</code> в очереди\n"
        f"💡 Статус: /ops",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )
