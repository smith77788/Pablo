"""
Account Cleaner UI — управление очисткой аккаунтов.

Entry: CleanerCb(action="menu")
"""

from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CleanerCb, BmCb
from database import db
from services.logger import log_exc_swallow
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()


async def _get_telethon_account(
    pool: asyncpg.Pool,
    account_id: int | None,
    owner_id: int,
) -> asyncpg.Record | None:
    if account_id is None:
        return None
    return await db.get_account_for_telethon(pool, account_id, owner_id)


async def _managed_channel_whitelist(pool: asyncpg.Pool, acc_id: int) -> list[str]:
    """Channel/group IDs this account administers in the system, normalized to
    the short positive form Telethon dialog entities use (managed_channels
    stores the full -100xxxxxxxxxx form) — passed as leave_all_chats'
    whitelist so "leave all chats" can't silently break channels the account
    is supposed to be posting to/administering.
    """
    try:
        rows = await pool.fetch(
            "SELECT channel_id FROM managed_channels WHERE acc_id=$1", acc_id
        )
    except Exception:
        return []
    out = []
    for r in rows:
        raw = str(abs(int(r["channel_id"])))
        out.append(raw[3:] if raw.startswith("100") and len(raw) > 10 else raw)
    return out


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))
    return kb


@router.callback_query(CleanerCb.filter(F.action == "menu"))
async def cb_cleaner_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚪 Выйти из всех чатов", callback_data=CleanerCb(action="leave_all")
    )
    kb.button(
        text="👥 Удалить контакты", callback_data=CleanerCb(action="del_contacts")
    )
    kb.button(
        text="📋 Список чатов аккаунта", callback_data=CleanerCb(action="list_chats")
    )
    kb.button(
        text="📭 Прочитать все диалоги", callback_data=CleanerCb(action="read_all")
    )
    kb.button(
        text="🗑 Удалить личные диалоги", callback_data=CleanerCb(action="del_pm")
    )
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🧹 <b>Account Cleaner — очистка аккаунтов</b>\n\n"
        "Инструменты для сброса аккаунта перед новым назначением:\n"
        "• <b>Выйти из всех чатов</b> — покинуть все группы и каналы\n"
        "• <b>Удалить контакты</b> — очистить список контактов\n"
        "• <b>Список чатов</b> — просмотр всех чатов аккаунта\n"
        "• <b>Прочитать все диалоги</b> — пометить непрочитанное прочитанным "
        "(снижает риск-сигналы аккаунта)\n"
        "• <b>Удалить личные диалоги</b> — очистить переписки в ЛС\n\n"
        "⚠️ <b>Осторожно:</b> действия необратимы!",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _pick_account_kb(
    pool: asyncpg.Pool, owner_id: int, action: str
) -> InlineKeyboardBuilder:
    try:
        accounts = await pool.fetch(
            """SELECT id, phone, first_name
               FROM tg_accounts
               WHERE owner_id=$1
                 AND is_active=TRUE
                 AND session_str IS NOT NULL
                 AND session_str <> ''
               ORDER BY added_at""",
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "_pick_account_kb fetch failed")
        accounts = []
    kb = InlineKeyboardBuilder()
    if not accounts:
        kb.button(
            text="📵 Нет аккаунтов с доступной сессией",
            callback_data=CleanerCb(action="menu"),
        )
    for acc in accounts:
        label = acc.get("first_name") or acc["phone"]
        kb.button(
            text=html.escape(label),
            callback_data=CleanerCb(action=action, account_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)
    return kb


@router.callback_query(CleanerCb.filter(F.action == "leave_all"))
async def cb_cleaner_leave_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    kb = await _pick_account_kb(pool, callback.from_user.id, "do_leave_all")
    await callback.message.edit_text(
        "🚪 <b>Выйти из всех чатов</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "del_contacts"))
async def cb_cleaner_del_contacts(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    kb = await _pick_account_kb(pool, callback.from_user.id, "confirm_del_contacts")
    await callback.message.edit_text(
        "👥 <b>Удалить контакты</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Прочитать все диалоги / удалить личные диалоги (паритет с mini-app) ─────────


@router.callback_query(CleanerCb.filter(F.action == "read_all"))
async def cb_cleaner_read_all(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    kb = await _pick_account_kb(pool, callback.from_user.id, "do_read_all")
    await callback.message.edit_text(
        "📭 <b>Прочитать все диалоги</b>\n\n"
        "Пометит непрочитанные чаты аккаунта прочитанными. Выберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "do_read_all"))
async def cb_cleaner_do_read_all(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    """Поставить в очередь read_all_dialogs (фоновая telethon-операция)."""
    acc_id = callback_data.account_id
    acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Ставлю в очередь…")
    from services import operation_bus

    label = acc.get("first_name") or acc["phone"]
    op_id = await operation_bus.submit(
        pool, callback.from_user.id, "read_all_dialogs", {"account_id": acc_id}
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📭 <b>Прочитать все диалоги</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"🆔 Операция: <b>#{op_id}</b> поставлена в очередь.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "del_pm"))
async def cb_cleaner_del_pm(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    kb = await _pick_account_kb(pool, callback.from_user.id, "confirm_del_pm")
    await callback.message.edit_text(
        "🗑 <b>Удалить личные диалоги</b>\n\n"
        "Удалит переписки в ЛС аккаунта. Выберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "confirm_del_pm"))
async def cb_cleaner_confirm_del_pm(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    """Подтверждение перед необратимым удалением личных диалогов."""
    await safe_answer(callback)
    acc_id = callback_data.account_id
    acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    label = acc.get("first_name") or acc["phone"]
    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚠️ Да, удалить личные диалоги",
        callback_data=CleanerCb(action="do_del_pm", account_id=acc_id),
    )
    kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🗑 <b>Удаление личных диалогов</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n\n"
        "⚠️ Все переписки в ЛС этого аккаунта будут удалены. Действие "
        "необратимо. Продолжить?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "do_del_pm"))
async def cb_cleaner_do_del_pm(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    """Поставить в очередь delete_private_dialogs (фоновая telethon-операция)."""
    acc_id = callback_data.account_id
    acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Ставлю в очередь…")
    from services import operation_bus

    label = acc.get("first_name") or acc["phone"]
    op_id = await operation_bus.submit(
        pool, callback.from_user.id, "delete_private_dialogs", {"account_id": acc_id}
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🗑 <b>Удалить личные диалоги</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"🆔 Операция: <b>#{op_id}</b> поставлена в очередь.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "confirm_del_contacts"))
async def cb_confirm_del_contacts(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    """Confirmation step before deleting all contacts."""
    await safe_answer(callback)
    acc_id = callback_data.account_id

    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
        )
    except Exception:
        log_exc_swallow(log, "confirm_del_contacts fetchrow failed")
        acc = None
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚠️ Да, удалить все контакты",
        callback_data=CleanerCb(action="do_del_contacts", account_id=acc_id),
    )
    kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"⚠️ <b>Подтвердите удаление контактов</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n\n"
        "Все контакты этого аккаунта будут удалены из Telegram.\n"
        "Это действие <b>необратимо</b>!",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "list_chats"))
async def cb_cleaner_list_chats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
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
    await safe_answer(callback)
    acc_id = callback_data.account_id

    # Confirm step
    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚠️ Да, выйти из ВСЕХ чатов",
        callback_data=CleanerCb(action="confirm_leave", account_id=acc_id),
    )
    kb.button(
        text="🔍 Сначала посмотреть список",
        callback_data=CleanerCb(action="dry_leave", account_id=acc_id),
    )
    kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)

    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
        )
    except Exception:
        log_exc_swallow(log, "do_leave_all fetchrow failed")
        acc = None
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

    try:
        acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "dry_leave fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return

    from services.account_cleaner import leave_all_chats

    whitelist = await _managed_channel_whitelist(pool, acc_id)
    result = await leave_all_chats(
        acc["session_str"], dict(acc), whitelist=whitelist, dry_run=True
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"⚠️ Выйти из {result['left']} чатов",
        callback_data=CleanerCb(action="confirm_leave", account_id=acc_id),
    )
    kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
    kb.adjust(1)

    protected_note = (
        f"🛡 Из них {len(whitelist)} управляемых каналов будут сохранены\n"
        if whitelist else ""
    )
    await callback.message.edit_text(
        f"📋 <b>Предварительный просмотр</b>\n\n"
        f"Будет покинуто: <b>{result['left']}</b> чатов\n"
        f"Пропущено: <b>{result['skipped']}</b>\n"
        f"{protected_note}\n"
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

    try:
        acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "confirm_leave fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return

    label = acc.get("first_name") or acc["phone"]

    # Guard: check for managed assets associated with this account
    try:
        asset_count = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM managed_channels WHERE acc_id=$1", acc_id
            )
            or 0
        )
    except Exception:
        asset_count = 0

    if asset_count > 0:
        kb = InlineKeyboardBuilder()
        kb.button(
            text=f"🛡 Выйти, оставив {asset_count} управляемых",
            callback_data=CleanerCb(action="leave_safe", account_id=acc_id),
        )
        kb.button(
            text="⚠️ Выйти вообще из всех, включая управляемые",
            callback_data=CleanerCb(action="force_leave", account_id=acc_id),
        )
        kb.button(text="❌ Отмена", callback_data=CleanerCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            f"⚠️ <b>Внимание: активы обнаружены!</b>\n\n"
            f"Аккаунт <b>{html.escape(label)}</b> управляет "
            f"<b>{asset_count}</b> каналами/группами в системе.\n\n"
            "Массовый выход может нарушить работу этих ресурсов. Можно "
            "выйти из всего остального, сохранив управляемые каналы за "
            "аккаунтом, либо выйти совсем из всего.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Guard: check for active operations in queue
    try:
        active_ops = (
            await pool.fetchval(
                """SELECT COUNT(*) FROM operation_queue
               WHERE owner_id=$1 AND status IN ('pending', 'running')""",
                callback.from_user.id,
            )
            or 0
        )
    except Exception:
        active_ops = 0

    if active_ops > 0:
        await callback.message.edit_text(
            f"⏳ <b>Операции в очереди</b>\n\n"
            f"Сейчас выполняется {active_ops} активных операций.\n"
            "Дождитесь завершения перед очисткой аккаунта.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    await _do_leave(callback.message, acc, pool)


async def _do_leave(
    message, acc: asyncpg.Record, pool: asyncpg.Pool, whitelist: list[str] | None = None
) -> None:
    """Execute leave_all_chats and display result."""
    label = acc.get("first_name") or acc["phone"]
    msg = await message.edit_text("⏳ Выхожу из чатов...")

    last_n = {"n": 0}

    async def progress(i: int, name: str) -> None:
        if i - last_n["n"] >= 5:
            last_n["n"] = i
            try:
                await msg.edit_text(
                    f"⏳ Выхожу из чатов... {i} обработано\nТекущий: {html.escape(name[:30])}"
                )
            except Exception:
                log_exc_swallow(log, "Ошибка обновления прогресса очистки аккаунта")

    from services.account_cleaner import leave_all_chats

    result = await leave_all_chats(
        acc["session_str"], dict(acc), whitelist=whitelist, progress_cb=progress
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=CleanerCb(action="menu"))

    await msg.edit_text(
        f"✅ <b>Очистка завершена</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"Покинуто чатов: <b>{result['left']}</b>\n"
        f"Пропущено: <b>{result['skipped']}</b>\n"
        + (f"Ошибок: <b>{len(result['errors'])}</b>" if result.get("errors") else ""),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CleanerCb.filter(F.action == "leave_safe"))
async def cb_leave_safe(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    """Leave all chats except this account's managed channels/groups."""
    await callback.answer("⏳ Очистка с защитой управляемых каналов...")
    acc_id = callback_data.account_id

    try:
        acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "leave_safe fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return

    whitelist = await _managed_channel_whitelist(pool, acc_id)
    await _do_leave(callback.message, acc, pool, whitelist=whitelist)


@router.callback_query(CleanerCb.filter(F.action == "force_leave"))
async def cb_force_leave(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    """Force leave all chats even if account has managed assets (user confirmed warning)."""
    await callback.answer("⏳ Принудительная очистка...")
    acc_id = callback_data.account_id

    try:
        acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "force_leave fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return

    await _do_leave(callback.message, acc, pool)


@router.callback_query(CleanerCb.filter(F.action == "do_del_contacts"))
async def cb_do_del_contacts(
    callback: CallbackQuery, callback_data: CleanerCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Удаляю контакты...")
    acc_id = callback_data.account_id

    try:
        acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "do_del_contacts fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
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

    try:
        acc = await _get_telethon_account(pool, acc_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "show_chats fetchrow failed")
        acc = None
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.", reply_markup=_back_kb().as_markup()
        )
        return

    from services.account_cleaner import get_chat_list_for_cleanup

    chats = await get_chat_list_for_cleanup(acc["session_str"], dict(acc))

    label = acc.get("first_name") or acc["phone"]
    lines = [
        f"📋 <b>Чаты аккаунта {html.escape(label)}</b>\n",
        f"Всего: {len(chats)}\n",
    ]

    type_icons = {"group": "👥", "channel": "📢", "pm": "💬"}
    for ch in chats[:25]:
        icon = type_icons.get(ch["type"], "❓")
        uname = f"@{ch['username']}" if ch.get("username") else ""
        members = f" ({ch['members']})" if ch.get("members") else ""
        lines.append(f"{icon} {html.escape(ch['title'][:30])}{uname}{members}")

    if len(chats) > 25:
        lines.append(f"\n<i>...и ещё {len(chats) - 25} чатов</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )
