"""Multi-user Workspace management — Enterprise tier."""
from __future__ import annotations
import logging
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import WorkspaceCb, BmCb
from bot.states import WorkspaceFSM
from bot.utils.subscription import require_plan
from database import db

router = Router()
log = logging.getLogger(__name__)


def _ws_main_kb() -> ...:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать workspace", callback_data=WorkspaceCb(action="create"))
    kb.button(text="🔗 Войти по коду",     callback_data=WorkspaceCb(action="join"))
    kb.button(text="⬅️ Назад",             callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _ws_view_kb(ws_id: int, is_owner: bool) -> ...:
    kb = InlineKeyboardBuilder()
    if is_owner:
        kb.button(text="👥 Участники",     callback_data=WorkspaceCb(action="members", ws_id=ws_id))
        kb.button(text="🔗 Пригласить",    callback_data=WorkspaceCb(action="invite",  ws_id=ws_id))
        kb.button(text="🗑 Удалить",       callback_data=WorkspaceCb(action="leave",   ws_id=ws_id))
    else:
        kb.button(text="👥 Участники",     callback_data=WorkspaceCb(action="members", ws_id=ws_id))
        kb.button(text="🚪 Покинуть",      callback_data=WorkspaceCb(action="leave",   ws_id=ws_id))
    kb.button(text="⬅️ Назад",             callback_data=WorkspaceCb(action="menu"))
    kb.adjust(2, 1)
    return kb.as_markup()


@router.callback_query(WorkspaceCb.filter(F.action == "menu"))
async def cb_ws_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        from bot.utils.subscription import locked_text
        from bot.keyboards import subscription_locked_markup
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Workspaces", "enterprise"),
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    await callback.answer()
    workspaces = await db.get_user_workspaces(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    for ws in workspaces:
        role_icon = "👑" if ws["role"] == "owner" else "👤"
        kb.button(
            text=f"{role_icon} {ws['name']} ({ws['member_count']} участ.)",
            callback_data=WorkspaceCb(action="view", ws_id=ws["id"]),
        )
    kb.button(text="➕ Создать workspace", callback_data=WorkspaceCb(action="create"))
    kb.button(text="🔗 Войти по коду",     callback_data=WorkspaceCb(action="join"))
    kb.button(text="⬅️ Назад",             callback_data=BmCb(action="main"))
    kb.adjust(1)
    text = (
        "🏢 <b>Workspaces</b>\n\n"
        "Пространства позволяют командам совместно управлять инфраструктурой.\n\n"
    )
    if workspaces:
        text += f"Вы участник <b>{len(workspaces)}</b> workspace(s):"
    else:
        text += "У вас пока нет workspaces."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(WorkspaceCb.filter(F.action == "view"))
async def cb_ws_view(callback: CallbackQuery, callback_data: WorkspaceCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    ws = await db.get_workspace(pool, callback_data.ws_id)
    if not ws:
        await callback.message.edit_text("❌ Workspace не найден.")
        return
    is_owner = ws["owner_id"] == callback.from_user.id
    members = await db.get_workspace_members(pool, callback_data.ws_id)
    role_map = {m["user_id"]: m["role"] for m in members}
    user_role = role_map.get(callback.from_user.id, "?")
    text = (
        f"🏢 <b>{ws['name']}</b>\n"
        f"Ваша роль: <code>{user_role}</code>\n"
        f"Участников: {len(members)}\n"
    )
    if ws.get("description"):
        text += f"\n{ws['description']}"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_ws_view_kb(callback_data.ws_id, is_owner)
    )


@router.callback_query(WorkspaceCb.filter(F.action == "members"))
async def cb_ws_members(callback: CallbackQuery, callback_data: WorkspaceCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    members = await db.get_workspace_members(pool, callback_data.ws_id)
    lines = []
    for m in members:
        name = m.get("first_name") or m.get("username") or str(m["user_id"])
        role_icon = {"owner": "👑", "admin": "⚙️", "member": "👤", "viewer": "👁"}.get(m["role"], "❓")
        lines.append(f"{role_icon} {name} — <code>{m['role']}</code>")
    text = "👥 <b>Участники workspace</b>\n\n" + ("\n".join(lines) if lines else "Нет участников.")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=WorkspaceCb(action="view", ws_id=callback_data.ws_id))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(WorkspaceCb.filter(F.action == "invite"))
async def cb_ws_invite(callback: CallbackQuery, callback_data: WorkspaceCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    code = await db.create_workspace_invite(pool, callback_data.ws_id, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=WorkspaceCb(action="view", ws_id=callback_data.ws_id))
    await callback.message.edit_text(
        f"🔗 <b>Ссылка-приглашение создана</b>\n\n"
        f"Код: <code>{code}</code>\n\n"
        f"До 5 пользователей могут войти по этому коду через меню Workspaces → Войти по коду.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WorkspaceCb.filter(F.action == "create"))
async def cb_ws_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(WorkspaceFSM.entering_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=WorkspaceCb(action="menu"))
    await callback.message.edit_text(
        "➕ <b>Создать Workspace</b>\n\nВведите название workspace (до 64 символов):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(WorkspaceFSM.entering_name)
async def msg_ws_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("❌ Название должно быть от 1 до 64 символов. Попробуйте снова:")
        return
    await state.update_data(ws_name=name)
    await state.set_state(WorkspaceFSM.entering_description)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=WorkspaceCb(action="create"))
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\nВведите описание (необязательно) или нажмите Пропустить:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(WorkspaceFSM.entering_description)
async def msg_ws_desc(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    description = (message.text or "").strip()[:256]
    data = await state.get_data()
    name = data.get("ws_name", "Workspace")
    await state.clear()
    ws_id = await db.create_workspace(pool, message.from_user.id, name, description)
    kb = InlineKeyboardBuilder()
    kb.button(text="🏢 Открыть workspace", callback_data=WorkspaceCb(action="view", ws_id=ws_id))
    kb.button(text="⬅️ К списку",          callback_data=WorkspaceCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Workspace создан!</b>\n\n🏢 {name}\n\nID: <code>{ws_id}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WorkspaceCb.filter(F.action == "join"))
async def cb_ws_join(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(WorkspaceFSM.entering_invite_code)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=WorkspaceCb(action="menu"))
    await callback.message.edit_text(
        "🔗 <b>Войти в Workspace</b>\n\nВведите код приглашения:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(WorkspaceFSM.entering_invite_code)
async def msg_ws_invite_code(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    code = (message.text or "").strip()
    await state.clear()
    ws_id = await db.use_workspace_invite(pool, code, message.from_user.id)
    if not ws_id:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Попробовать снова", callback_data=WorkspaceCb(action="join"))
        kb.button(text="⬅️ Назад",             callback_data=WorkspaceCb(action="menu"))
        kb.adjust(1)
        await message.answer(
            "❌ Неверный или истёкший код приглашения.",
            reply_markup=kb.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🏢 Открыть workspace", callback_data=WorkspaceCb(action="view", ws_id=ws_id))
    kb.adjust(1)
    await message.answer(
        "✅ <b>Вы вошли в workspace!</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WorkspaceCb.filter(F.action == "leave"))
async def cb_ws_leave(callback: CallbackQuery, callback_data: WorkspaceCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    ws = await db.get_workspace(pool, callback_data.ws_id)
    if ws and ws["owner_id"] == callback.from_user.id:
        await callback.message.edit_text(
            "⚠️ Вы являетесь <b>владельцем</b> этого workspace. Владелец не может покинуть его.",
            parse_mode="HTML",
        )
        return
    await db.delete_workspace_member(pool, callback_data.ws_id, callback.from_user.id)
    await callback.message.edit_text("✅ Вы покинули workspace.")
