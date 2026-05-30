"""
Account Cleaner UI — управление очисткой аккаунтов.

Entry: CleanerCb(action="menu")
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CleanerCb, BmCb
from bot.states import CleanerFSM
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))
    return kb


@router.callback_query(CleanerCb.filter(F.action == "menu"))
async def cb_cleaner_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="🚪 Выйти из всех чатов",   callback_data=CleanerCb(action="leave_all"))
    kb.button(text="👥 Удалить контакты",        callback_data=CleanerCb(action="del_contacts"))
    kb.button(text="📋 Список чатов аккаунта",   callback_data=CleanerCb(action="list_chats"))
    kb.button(text="◀️ Назад",                   callback_data=BmCb(action="infrastructure"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🧹 <b>Account Cleaner — очистка аккаунтов</b>\n\n"
        "Инструменты для сброса аккаунта перед новым назначением:\n"
        "• <b>Выйти из всех чатов</b> — покинуть все группы и каналы\n"
        "• <b>Удалить контакты</b> — очистить список контактов\n"
        "• <b>Список чатов</b> — просмотр всех чатов аккаунта\n\n"
        "⚠️ <b>Осторожно:</b> действия необратимы!",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _pick_account_kb(pool: asyncpg.Pool, owner_id: int, action: str) -> InlineKeyboardBuilder:
    accounts = await pool.fetch(
        "SELECT id, phone, first_name FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE ORDER BY added_at",
        owner_id,
    )
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        label = acc.get("first_name") or acc["phone"]
        kb.button(text=html.escape(label), callback_data=CleanerCb(action=action, account_id=acc["id"]))
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)
    return kb


@router.callback_query(CleanerCb.filter(F.action == "leave_all"))
async def cb_cleaner_leave_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    kb = await _pick_account_kb(pool, callback.from_user.id, "do_leave_all")
    await callback.message.edit_text(
        "🚪 <b>Выйти из всех чатов</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "del_contacts"))
async def cb_cleaner_del_contacts(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    kb = await _pick_account_kb(pool, callback.from_user.id, "do_del_contacts")
    await callback.message.edit_text(
        "👥 <b>Удалить контакты</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "list_chats"))
async def cb_cleaner_list_chats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    kb = await _pick_account_kb(pool, callback.from_user.id, "show_chats")
    await callback.message.edit_text(
        "📋 <b>Просмотр чатов</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "do_leave_all"))
async def cb_do_leave_all(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = callback_data.account_id

    # Confirm step
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Да, выйти из ВСЕХ чатов", callback_data=CleanerCb(action="confirm_leave", account_id=acc_id))
    kb.button(text="🔍 Сначала посмотреть список", callback_data=CleanerCb(action="dry_leave", account_id=acc_id))
    kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)

    acc = await pool.fetchrow("SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id)
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    await callback.message.edit_text(
        f"⚠️ <b>Подтвердите действие</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n\n"
        "Будут покинуты <b>все группы и каналы</b> этого аккаунта.\n"
        "Это необратимо!",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "dry_leave"))
async def cb_dry_leave(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю список чатов...")
    acc_id = callback_data.account_id

    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version, phone, first_name "
        "FROM tg_accounts WHERE id=$1",
        acc_id,
    )
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return

    from services.account_cleaner import leave_all_chats
    result = await leave_all_chats(acc["session_str"], dict(acc), dry_run=True)

    kb = InlineKeyboardBuilder()
    kb.button(text=f"⚠️ Выйти из {result['left']} чатов", callback_data=CleanerCb(action="confirm_leave", account_id=acc_id))
    kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"📋 <b>Предварительный просмотр</b>\n\n"
        f"Будет покинуто: <b>{result['left']}</b> чатов\n"
        f"Пропущено: <b>{result['skipped']}</b>\n\n"
        "Подтвердите выход:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "confirm_leave"))
async def cb_confirm_leave(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю...")
    acc_id = callback_data.account_id

    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version, phone, first_name "
        "FROM tg_accounts WHERE id=$1",
        acc_id,
    )
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return

    msg = await callback.message.edit_text("⏳ Выхожу из чатов...")

    last_n = {"n": 0}

    async def progress(i: int, name: str) -> None:
        if i - last_n["n"] >= 5:
            last_n["n"] = i
            try:
                await msg.edit_text(f"⏳ Выхожу из чатов... {i} обработано\nТекущий: {html.escape(name[:30])}")
            except Exception:
                log_exc_swallow(log, "Ошибка обновления прогресса очистки аккаунта")

    from services.account_cleaner import leave_all_chats
    result = await leave_all_chats(acc["session_str"], dict(acc), progress_cb=progress)

    label = acc.get("first_name") or acc["phone"]
    await msg.edit_text(
        f"✅ <b>Очистка завершена</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"Покинуто чатов: <b>{result['left']}</b>\n"
        f"Пропущено: <b>{result['skipped']}</b>\n"
        + (f"Ошибок: <b>{len(result['errors'])}</b>" if result.get("errors") else ""),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "do_del_contacts"))
async def cb_do_del_contacts(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Удаляю контакты...")
    acc_id = callback_data.account_id

    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version, phone, first_name "
        "FROM tg_accounts WHERE id=$1",
        acc_id,
    )
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return

    from services.account_cleaner import delete_contacts
    result = await delete_contacts(acc["session_str"], dict(acc))
    label = acc.get("first_name") or acc["phone"]

    await callback.message.edit_text(
        f"✅ <b>Контакты удалены</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"Удалено контактов: <b>{result['deleted']}</b>",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "show_chats"))
async def cb_show_chats(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Загружаю...")
    acc_id = callback_data.account_id

    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version, phone, first_name "
        "FROM tg_accounts WHERE id=$1",
        acc_id,
    )
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup())
        return

    from services.account_cleaner import get_chat_list_for_cleanup
    chats = await get_chat_list_for_cleanup(acc["session_str"], dict(acc))

    label = acc.get("first_name") or acc["phone"]
    lines = [f"📋 <b>Чаты аккаунта {html.escape(label)}</b>\n", f"Всего: {len(chats)}\n"]

    type_icons = {"group": "👥", "channel": "📢", "pm": "💬"}
    for ch in chats[:25]:
        icon = type_icons.get(ch["type"], "❓")
        uname = f"@{ch['username']}" if ch.get("username") else ""
        members = f" ({ch['members']})" if ch.get("members") else ""
        lines.append(f"{icon} {html.escape(ch['title'][:30])}{uname}{members}")

    if len(chats) > 25:
        lines.append(f"\n<i>...и ещё {len(chats)-25} чатов</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )
