"""Admin user management: list, grant/revoke plans, ban/unban."""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
import asyncpg
from bot.callbacks import CallbackData
from bot.utils.subscription import is_platform_admin
from database import db
from services.logger import log_exc_swallow


def _is_admin(uid: int) -> bool:
    """Check admin status using both env ADMIN_IDS and session admins."""
    try:
        from bot.handlers.admin import is_admin

        return is_admin(uid)
    except Exception:
        log_exc_swallow(log, "Ошибка проверки is_admin через admin.py")
        return is_platform_admin(uid)


log = logging.getLogger(__name__)
router = Router()


class AdminUserCb(CallbackData, prefix="admu"):
    action: str
    user_id: int = 0
    page: int = 0
    plan: str = ""
    months: int = 0


class AdminUserFSM(StatesGroup):
    choosing_months = State()


def _format_plan_emoji(plan: str) -> str:
    emojis = {"free": "🆓", "starter": "⭐", "pro": "🚀", "enterprise": "👑"}
    return emojis.get(plan, "❓")


async def _users_list_text(
    pool: asyncpg.Pool, page: int = 0, items_per_page: int = 5
) -> tuple[str, int]:
    """Вернуть текст списка пользователей и общее количество."""
    total = await db.count_platform_users(pool)
    users = await db.get_all_platform_users(
        pool, limit=items_per_page, offset=page * items_per_page
    )

    text = f"👥 <b>Все пользователи</b> (всего: {total})\n\n"

    if not users:
        return text + "Нет пользователей.", total

    for u in users:
        emoji = _format_plan_emoji(u["current_plan"])
        expires = ""
        if u["plan_expires_at"]:
            from datetime import timezone

            exp = u["plan_expires_at"]
            # asyncpg returns timezone-aware datetimes from TIMESTAMPTZ columns
            from datetime import datetime

            now = datetime.now(timezone.utc)
            exp_aware = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
            days_left = (exp_aware - now).days
            expires = f" (истекает через {days_left}д)" if days_left > 0 else " (ИСТЁК)"

        banned_mark = "🚫 " if u["is_banned"] else ""
        username = u["username"] or f"#{u['user_id']}"
        reg_dt = u.get("registered_at")
        reg_str = reg_dt.strftime('%d.%m.%y') if reg_dt else "—"
        text += (
            f"{banned_mark}{emoji} <b>@{username}</b>\n"
            f"  ID: <code>{u['user_id']}</code>\n"
            f"  План: {u['current_plan'].upper()}{expires}\n"
            f"  Зарег: {reg_str}\n\n"
        )

    return text, total


@router.callback_query(AdminUserCb.filter(F.action == "list"))
async def cb_users_list(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Показать список пользователей."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Только администратор", show_alert=True)
        return

    await callback.answer()
    page = callback_data.page

    text, total = await _users_list_text(pool, page)
    max_page = (total - 1) // 5

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    # Навигация
    if page > 0:
        kb.button(
            text="⬅️ Назад", callback_data=AdminUserCb(action="list", page=page - 1)
        )
    kb.button(
        text=f"📄 {page + 1}/{max_page + 1}",
        callback_data=AdminUserCb(action="list", page=page),
    )
    if page < max_page:
        kb.button(
            text="➡️ Вперёд", callback_data=AdminUserCb(action="list", page=page + 1)
        )

    kb.adjust(1)
    kb.button(text="🔍 Поиск по плану", callback_data=AdminUserCb(action="filter_plan"))
    kb.button(text="🚫 Забаненные", callback_data=AdminUserCb(action="banned_list"))
    kb.button(text="◀️ В админ-меню", callback_data=AdminUserCb(action="main_menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminUserCb.filter(F.action == "filter_plan"))
async def cb_filter_plan(callback: CallbackQuery) -> None:
    """Меню фильтра по плану."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    await callback.answer()
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    plans = [
        ("🆓 Free", "free"),
        ("⭐ Starter", "starter"),
        ("🚀 Pro", "pro"),
        ("👑 Enterprise", "enterprise"),
    ]
    for label, plan in plans:
        kb.button(
            text=label, callback_data=AdminUserCb(action="plan_list", plan=plan, page=0)
        )

    kb.button(text="◀️ Назад", callback_data=AdminUserCb(action="list", page=0))
    kb.adjust(1)
    await callback.message.edit_text(
        "Выберите план для фильтра:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminUserCb.filter(F.action == "plan_list"))
async def cb_plan_list(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Показать пользователей с конкретным планом."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    await callback.answer()
    page = callback_data.page
    plan = callback_data.plan

    total = await db.count_platform_users(pool, plan=plan)
    users = await db.get_all_platform_users(pool, limit=5, offset=page * 5, plan=plan)

    text = f"<b>{_format_plan_emoji(plan)} Пользователи плана {plan.upper()}</b> (всего: {total})\n\n"
    if not users:
        text += "Нет пользователей с этим планом."
    else:
        for u in users:
            username = u["username"] or f"#{u['user_id']}"
            text += f"@{username} (<code>{u['user_id']}</code>)\n"

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    if page > 0:
        kb.button(
            text="⬅️",
            callback_data=AdminUserCb(action="plan_list", plan=plan, page=page - 1),
        )
    kb.button(
        text=f"{page + 1}",
        callback_data=AdminUserCb(action="plan_list", plan=plan, page=page),
    )
    max_page = (total - 1) // 5
    if page < max_page:
        kb.button(
            text="➡️",
            callback_data=AdminUserCb(action="plan_list", plan=plan, page=page + 1),
        )

    kb.adjust(3)
    kb.button(text="◀️ Назад", callback_data=AdminUserCb(action="filter_plan"))
    kb.adjust(1)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminUserCb.filter(F.action == "banned_list"))
async def cb_banned_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Показать забаненных пользователей."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    await callback.answer()
    users = await db.get_all_platform_users(pool, limit=20, is_banned=True)

    text = f"🚫 <b>Забаненные пользователи</b> (всего: {len(users)})\n\n"
    if not users:
        text += "Нет забаненных пользователей."
    else:
        for u in users:
            username = u["username"] or f"#{u['user_id']}"
            text += f"🚫 <b>@{username}</b>\n  ID: <code>{u['user_id']}</code>\n\n"

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=AdminUserCb(action="list", page=0))
    kb.adjust(1)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminUserCb.filter(F.action == "user_actions"))
async def cb_user_actions(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Показать действия над пользователем."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    user = await db.get_user_info(pool, user_id)

    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    await callback.answer()

    username = user["username"] or f"#{user_id}"
    emoji = _format_plan_emoji(user["current_plan"])

    text = (
        f"👤 <b>@{username}</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"План: {emoji} {user['current_plan'].upper()}\n"
        f"Статус: {'🚫 Забанен' if user['is_banned'] else '✅ Активен'}\n\n"
        f"<b>Выберите действие:</b>"
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    kb.button(
        text="💳 Выдать подписку",
        callback_data=AdminUserCb(action="grant_plan", user_id=user_id),
    )
    kb.button(
        text="❌ Забрать подписку",
        callback_data=AdminUserCb(action="revoke_plan", user_id=user_id),
    )
    kb.button(
        text="⚔️ Выдать Strike",
        callback_data=AdminUserCb(action="grant_strike", user_id=user_id),
    )
    kb.button(
        text="⚔️ Забрать Strike",
        callback_data=AdminUserCb(action="revoke_strike", user_id=user_id),
    )

    if user["is_banned"]:
        kb.button(
            text="✅ Разбанить",
            callback_data=AdminUserCb(action="unban", user_id=user_id),
        )
    else:
        kb.button(
            text="🚫 Забанить", callback_data=AdminUserCb(action="ban", user_id=user_id)
        )

    kb.button(text="◀️ Назад", callback_data=AdminUserCb(action="list", page=0))
    kb.adjust(1)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminUserCb.filter(F.action == "grant_plan"))
async def cb_grant_plan(
    callback: CallbackQuery, callback_data: AdminUserCb, state: FSMContext
) -> None:
    """Меню выбора плана для выдачи."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    await callback.answer()
    await state.set_state(AdminUserFSM.choosing_months)
    await state.update_data(user_id=callback_data.user_id)

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    plans = [
        ("🆓 Free", "free"),
        ("⭐ Starter", "starter"),
        ("🚀 Pro", "pro"),
        ("👑 Enterprise", "enterprise"),
    ]
    for label, plan in plans:
        kb.button(
            text=label,
            callback_data=AdminUserCb(
                action="plan_months", user_id=callback_data.user_id, plan=plan
            ),
        )

    kb.button(
        text="◀️ Назад",
        callback_data=AdminUserCb(action="user_actions", user_id=callback_data.user_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        "Выберите план для выдачи:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminUserCb.filter(F.action == "plan_months"))
async def cb_plan_months(
    callback: CallbackQuery, callback_data: AdminUserCb, state: FSMContext
) -> None:
    """Меню выбора срока подписки."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    await callback.answer()
    await state.update_data(plan=callback_data.plan)

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    months_options = [
        (1, "1 месяц"),
        (3, "3 месяца"),
        (6, "6 месяцев"),
        (12, "12 месяцев"),
    ]
    for months, label in months_options:
        kb.button(
            text=label,
            callback_data=AdminUserCb(
                action="confirm_grant",
                user_id=callback_data.user_id,
                plan=callback_data.plan,
                months=months,
            ),
        )

    kb.button(
        text="◀️ Назад",
        callback_data=AdminUserCb(action="grant_plan", user_id=callback_data.user_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        f"Выберите срок для плана {callback_data.plan.upper()}:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AdminUserCb.filter(F.action == "confirm_grant"))
async def cb_confirm_grant(
    callback: CallbackQuery,
    callback_data: AdminUserCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    """Подтвердить выдачу плана."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    plan = callback_data.plan
    months = callback_data.months

    await db.grant_plan_to_user(pool, user_id, callback.from_user.id, plan, months)
    await callback.answer(f"✅ План {plan} выдан на {months} мес.", show_alert=True)
    await state.clear()

    # Вернуться к меню пользователя
    await callback.message.edit_text(
        f"✅ План <b>{plan.upper()}</b> выдан пользователю #{user_id} на {months} месяцев.",
        parse_mode="HTML",
    )


@router.callback_query(AdminUserCb.filter(F.action == "revoke_plan"))
async def cb_revoke_plan(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Забрать подписку у пользователя."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    await db.revoke_plan_from_user(pool, user_id, callback.from_user.id)
    await callback.answer(
        "✅ Подписка отменена. Пользователь вернулся на free.", show_alert=True
    )

    # Вернуться к меню пользователя
    await callback.message.edit_text(
        f"✅ Подписка отменена. Пользователь #{user_id} переведён на план <b>FREE</b>.",
        parse_mode="HTML",
    )


@router.callback_query(AdminUserCb.filter(F.action == "grant_strike"))
async def cb_grant_strike(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Выдать Strike доступ пользователю."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    from bot.handlers.strike import _ensure_table

    await _ensure_table(pool)
    await pool.execute(
        "INSERT INTO strike_access (user_id, granted_by) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO NOTHING",
        user_id,
        callback.from_user.id,
    )
    await callback.answer("✅ Strike доступ выдан.", show_alert=True)

    await callback.message.edit_text(
        f"⚔️ Strike доступ выдан пользователю #{user_id}.",
        parse_mode="HTML",
    )
    try:
        await callback.bot.send_message(
            user_id,
            "⚔️ <b>Strike Module активирован!</b>\n\n"
            "Администратор предоставил вам доступ к Strike Module.\n"
            "Откройте меню для использования.",
            parse_mode="HTML",
        )
    except Exception:
        log_exc_swallow(log, "Ошибка отправки уведомления об активации Strike-доступа")


@router.callback_query(AdminUserCb.filter(F.action == "revoke_strike"))
async def cb_revoke_strike(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Забрать Strike доступ у пользователя."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    await db.revoke_strike_access(pool, user_id, callback.from_user.id)
    await callback.answer("✅ Strike доступ отозван.", show_alert=True)

    await callback.message.edit_text(
        f"⚔️ Strike доступ отозван у пользователя #{user_id}.",
        parse_mode="HTML",
    )
    try:
        await callback.bot.send_message(
            user_id,
            "ℹ️ <b>Strike доступ был отозван администратором.</b>\n\n"
            "Для получения доступа обратитесь к администратору.",
            parse_mode="HTML",
        )
    except Exception:
        log_exc_swallow(log, "Ошибка отправки уведомления об отзыве Strike-доступа")


@router.callback_query(AdminUserCb.filter(F.action == "ban"))
async def cb_ban(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Забанить пользователя."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    await db.ban_user(pool, user_id, callback.from_user.id, "Забанен администратором")
    await callback.answer(f"✅ Пользователь #{user_id} забанен.", show_alert=True)


@router.callback_query(AdminUserCb.filter(F.action == "unban"))
async def cb_unban(
    callback: CallbackQuery, callback_data: AdminUserCb, pool: asyncpg.Pool
) -> None:
    """Разбанить пользователя."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    user_id = callback_data.user_id
    await db.unban_user(pool, user_id, callback.from_user.id)
    await callback.answer(f"✅ Пользователь #{user_id} разбанен.", show_alert=True)


@router.callback_query(AdminUserCb.filter(F.action == "export_csv"))
async def cb_export_csv_users(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Экспорт всех пользователей платформы в CSV-файл."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Только администратор", show_alert=True)
        return

    await callback.answer("⏳ Генерирую CSV…")

    try:
        users = await db.get_all_platform_users(pool, limit=10000, offset=0)
    except Exception as e:
        log_exc_swallow(log, "Ошибка экспорта CSV пользователей")
        await callback.message.answer(f"❌ Ошибка: {e}")
        return

    import csv
    import io
    from aiogram.types import BufferedInputFile

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "user_id",
            "username",
            "first_name",
            "current_plan",
            "is_banned",
            "registered_at",
            "plan_expires_at",
        ]
    )
    for u in users:
        writer.writerow(
            [
                u["user_id"],
                u["username"] or "",
                u["first_name"] or "",
                u["current_plan"] or "free",
                "да" if u["is_banned"] else "нет",
                u["registered_at"].strftime("%Y-%m-%d %H:%M")
                if u.get("registered_at")
                else "",
                u["plan_expires_at"].strftime("%Y-%m-%d %H:%M")
                if u.get("plan_expires_at")
                else "",
            ]
        )

    data = buf.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(data, filename="platform_users.csv")
    await callback.message.answer_document(
        file,
        caption=f"📥 <b>Все пользователи платформы</b>\n{len(users)} записей",
        parse_mode="HTML",
    )


@router.callback_query(AdminUserCb.filter(F.action == "main_menu"))
async def cb_main_menu(callback: CallbackQuery) -> None:
    """Вернуться в админ-меню."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return

    await callback.answer()

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пользователи", callback_data=AdminUserCb(action="list"))
    kb.button(text="🔐 Аудит логи", callback_data="adm:audit_log")
    kb.button(text="◀️ Главное меню", callback_data="adm:main")
    kb.adjust(1)

    await callback.message.edit_text(
        "⚙️ <b>Управление пользователями</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
