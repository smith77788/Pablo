"""Super-admin panel — platform management, monitoring, token vault, user control."""

from __future__ import annotations
import asyncio
import csv
import html as _html
import io
import logging
import os
from datetime import datetime, timezone

import asyncpg
import aiohttp
from aiogram import Router, F
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiogram.filters import Command
from bot.keyboards import main_menu
from bot.utils.subscription import get_free_mode, set_free_mode
from config import ADMIN_SECRET
from database import db
from services import railway_api
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_NOTIFY_NEW_USERS = True  # toggle via /admin toggle_notify

# Пользователи, вошедшие через ADMIN_SECRET в текущей сессии бота
_session_admins: set[int] = set()


def _is_admin(uid: int) -> bool:
    # Сессионный доступ (через секретную фразу)
    if uid in _session_admins:
        return True
    # Постоянный доступ (через ADMIN_IDS в env — читаем динамически)
    raw = os.getenv("ADMIN_IDS", "")
    ids = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}
    return bool(ids) and uid in ids


def is_admin(uid: int) -> bool:
    """Public alias for _is_admin — checks both env ADMIN_IDS and session admins."""
    return _is_admin(uid)


# ── Keyboards ─────────────────────────────────────────────────────────────────


def _legacy_admin_main_kb(new_error_reports: int = 0):
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пользователи платформы", callback_data="adm:users")
    kb.button(text="💳 Подписки & платежи", callback_data="adm:subs")
    kb.button(text="🤖 Все боты & токены", callback_data="adm:bots")
    kb.button(text="📊 Системная статистика", callback_data="adm:stats")
    kb.button(text="📈 Операции платформы", callback_data="adm:platform_ops")
    kb.button(text="💰 Цены на подписки", callback_data="adm:prices")
    kb.button(text="⚙️ Методы оплаты", callback_data="adm:pay_cfg")
    kb.button(text="📨 Рассылка всем юзерам", callback_data="adm:broadcast")
    _notify_icon = "✅" if _NOTIFY_NEW_USERS else "❌"
    kb.button(
        text=f"🔔 Уведомления о новых {_notify_icon}", callback_data="adm:notify_toggle"
    )
    _free_icon = "✅ ВКЛ" if get_free_mode() else "❌ ВЫКЛ"
    kb.button(text=f"🆓 Free Mode: {_free_icon}", callback_data="adm:free_mode_toggle")
    kb.button(text="🚫 Заблокировать юзера", callback_data="adm:block_ask")
    kb.button(text="✅ Разблокировать юзера", callback_data="adm:unblock_ask")
    kb.button(text="🗑 Удалить данные юзера", callback_data="adm:delete_ask")
    kb.button(text="💰 Выдать подписку", callback_data="adm:grant_ask")
    kb.button(text="❌ Забрать подписку", callback_data="adm:revoke_ask")
    kb.button(text="💰 Bulk-выдача подписок", callback_data="adm:bulk_grant_ask")
    kb.button(text="⚔️ Выдать Strike доступ", callback_data="adm:strike_grant_ask")
    kb.button(text="⚔️ Забрать Strike доступ", callback_data="adm:strike_revoke_ask")
    kb.button(text="📁 Экспорт токенов (файл)", callback_data="adm:tokens_file")
    kb.button(text="📋 Экспорт юзеров (CSV)", callback_data="adm:users_csv")
    kb.button(text="🔍 Поиск юзера", callback_data="adm:find_user")
    kb.button(text="⚙️ Системный режим Swarm", callback_data="adm:swarm_mode")
    kb.button(text="🧹 Очистка данных", callback_data="adm:cleanup_ask")
    kb.button(text="🔑 Переменные Railway", callback_data="adm:env_list")
    _err_label = (
        f"🐛 Отчёты об ошибках ({new_error_reports} новых)"
        if new_error_reports > 0
        else "🐛 Отчёты об ошибках"
    )
    kb.button(text=_err_label, callback_data="adm:error_reports")
    kb.button(text="◀️ Выйти из админки", callback_data="adm:exit")
    kb.adjust(2)
    return kb.as_markup()


# ── Список отображаемых переменных (с метками) ────────────────────────────────

# Structured admin dashboard. The legacy keyboard above is intentionally kept as a
# fallback reference while the live panel uses compact role-based sections.


def _admin_main_kb(new_error_reports: int = 0):
    kb = InlineKeyboardBuilder()
    err_label = (
        f"🐛 Ошибки ({new_error_reports})" if new_error_reports > 0 else "🐛 Ошибки"
    )
    kb.button(text="📊 Обзор", callback_data="adm:main")
    kb.button(text="👥 Пользователи", callback_data="adm:section_users")
    kb.button(text="💳 Деньги", callback_data="adm:section_billing")
    kb.button(text="🤖 Боты / токены", callback_data="adm:section_assets")
    kb.button(text="⚙️ Операции", callback_data="adm:section_ops")
    kb.button(text="🧠 AI / провайдеры", callback_data="adm:section_ai")
    kb.button(text="🛠 Система", callback_data="adm:section_system")
    kb.button(text=err_label, callback_data="adm:error_reports")
    kb.button(text="🚪 Выйти", callback_data="adm:exit")
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def _admin_section_kb(section: str, new_error_reports: int = 0):
    kb = InlineKeyboardBuilder()
    if section == "users":
        kb.button(text="👥 Последние пользователи", callback_data="adm:users")
        kb.button(text="🔍 Найти юзера", callback_data="adm:find_user")
        kb.button(text="🚫 Заблокировать", callback_data="adm:block_ask")
        kb.button(text="✅ Разблокировать", callback_data="adm:unblock_ask")
        kb.button(text="🗑 Удалить данные", callback_data="adm:delete_ask")
        kb.button(text="📋 Экспорт CSV", callback_data="adm:users_csv")
        kb.button(text="🔔 Уведомления", callback_data="adm:notify_toggle")
        kb.adjust(2, 2, 2, 1)
    elif section == "billing":
        kb.button(text="💳 Активные подписки", callback_data="adm:subs")
        kb.button(text="💰 Выдать подписку", callback_data="adm:grant_ask")
        kb.button(text="❌ Забрать подписку", callback_data="adm:revoke_ask")
        kb.button(text="💰 Bulk-выдача", callback_data="adm:bulk_grant_ask")
        kb.button(text="💵 Цены", callback_data="adm:prices")
        kb.button(text="⚙️ Методы оплаты", callback_data="adm:pay_cfg")
        kb.adjust(2, 2, 2)
    elif section == "assets":
        kb.button(text="🤖 Все боты", callback_data="adm:bots")
        kb.button(text="📁 Экспорт токенов", callback_data="adm:tokens_file")
        kb.button(text="⚔️ Выдать Strike", callback_data="adm:strike_grant_ask")
        kb.button(text="⚔️ Забрать Strike", callback_data="adm:strike_revoke_ask")
        kb.button(text="📨 Рассылка всем", callback_data="adm:broadcast")
        kb.adjust(2, 2, 1)
    elif section == "ops":
        kb.button(text="📊 Логи действий", callback_data="adm:logs")
        kb.button(text="📈 Очередь операций", callback_data="adm:platform_ops")
        kb.button(text="🔐 Аудит TG-операций", callback_data="adm:audit_log")
        kb.button(text="📊 Системная статистика", callback_data="adm:stats")
        kb.button(text="🧹 Очистка данных", callback_data="adm:cleanup_ask")
        kb.adjust(1, 2, 2)
    elif section == "ai":
        kb.button(text="🧠 Статус AI", callback_data="adm:ai_status")
        kb.button(text="🔑 Переменные AI", callback_data="adm:env_list")
        kb.button(text="⚙️ Swarm режим", callback_data="adm:swarm_mode")
        kb.adjust(1)
    elif section == "system":
        free_icon = "✅ ВКЛ" if get_free_mode() else "❌ ВЫКЛ"
        notify_icon = "✅" if _NOTIFY_NEW_USERS else "❌"
        kb.button(
            text=f"🆓 Free Mode: {free_icon}", callback_data="adm:free_mode_toggle"
        )
        kb.button(
            text=f"🔔 Новые пользователи: {notify_icon}",
            callback_data="adm:notify_toggle",
        )
        kb.button(text="🔑 Railway env", callback_data="adm:env_list")
        kb.button(text="⚙️ Swarm режим", callback_data="adm:swarm_mode")
        err_label = (
            f"🐛 Отчёты ({new_error_reports})"
            if new_error_reports > 0
            else "🐛 Отчёты об ошибках"
        )
        kb.button(text=err_label, callback_data="adm:error_reports")
        kb.adjust(1)
    kb.button(text="🏠 Админка", callback_data="adm:main")
    kb.adjust(1)
    return kb.as_markup()


_ENV_VARS: list[tuple[str, str]] = [
    ("AI_PROVIDER_ORDER", "🧠 AI Provider Order"),
    ("OPENROUTER_MODELS", "🧠 OpenRouter Models"),
    ("GROQ_API_KEY", "🧠 Groq Key"),
    ("GROQ_MODEL", "🧠 Groq Model"),
    ("GEMINI_API_KEY", "🧠 Gemini Key"),
    ("GEMINI_MODEL", "🧠 Gemini Model"),
    ("MANAGER_BOT_TOKEN", "🤖 Bot Token"),
    ("ADMIN_IDS", "👑 Admin IDs"),
    ("ADMIN_SECRET", "🔐 Admin Secret"),
    ("TON_WALLET", "💎 TON Wallet"),
    ("TON_API_KEY", "🔑 TON API Key"),
    ("TRON_WALLET", "💵 TRON Wallet"),
    ("TRON_API_KEY", "🔑 TRON API Key"),
    ("OPENROUTER_API_KEY", "🤖 OpenRouter Key"),
    ("OPENROUTER_MODEL", "🧠 OpenRouter Model"),
    ("ANTHROPIC_API_KEY", "🧠 Anthropic Key"),
    ("TG_API_ID", "📱 TG API ID"),
    ("TG_API_HASH", "📱 TG API Hash"),
    ("BROADCAST_DELAY", "⏱ Broadcast Delay"),
    ("RAILWAY_TOKEN", "🚂 Railway Token"),
    ("RAILWAY_PROJECT_ID", "🚂 Railway Project ID"),
]
_ENV_KEYS = {k for k, _ in _ENV_VARS}


def _env_list_kb(vars_online: dict[str, str] | None = None):
    kb = InlineKeyboardBuilder()
    for key, label in _ENV_VARS:
        val = (vars_online or {}).get(key, "")
        status = "✅" if val else "❌"
        kb.button(text=f"{status} {label}", callback_data=f"adm:env_edit:{key}")
    kb.button(text="➕ Добавить переменную", callback_data="adm:env_add")
    kb.button(text="🔄 Обновить список", callback_data="adm:env_list")
    kb.button(text="◀️ Главное меню админки", callback_data="adm:main")
    kb.adjust(1)
    return kb.as_markup()


def _back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Главное меню админки", callback_data="adm:main")
    return kb.as_markup()


# ── /admin команда ─────────────────────────────────────────────────────────────


@router.message(Command("admin"))
async def cmd_admin(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    if _is_admin(uid):
        await _show_admin_main(message, pool, edit=False)
        return
    # Если ADMIN_IDS пустой но ADMIN_SECRET задан — подсказываем как войти
    if ADMIN_SECRET:
        await message.answer(
            "🔑 <b>Введите секретную фразу для доступа к AdminPanel</b>\n\n"
            f"Ваш ID: <code>{uid}</code>",
            parse_mode="HTML",
        )
    # Иначе — молчим (не раскрываем существование команды)


# ── Секретная фраза для входа в админку ────────────────────────────────────────


@router.message(F.text.func(lambda t: bool(ADMIN_SECRET) and t == ADMIN_SECRET))
async def cmd_admin_secret(message: Message) -> None:
    # Секретная фраза совпала — даём сессионный доступ без проверки ADMIN_IDS
    _session_admins.add(message.from_user.id)
    try:
        await message.delete()
    except Exception:
        log_exc_swallow(
            log,
            "Не удалось удалить сообщение с секретной фразой",
            user_id=message.from_user.id,
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Открыть Админ Меню", callback_data="adm:main")
    await message.answer(
        f"🔑 Доступ предоставлен\n\n"
        f"💡 Ваш ID: <code>{message.from_user.id}</code>\n"
        f"Добавьте его в переменную <code>ADMIN_IDS</code> через Railway чтобы зафиксировать доступ.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _show_admin_main(msg_or_cb, pool: asyncpg.Pool, edit: bool = True) -> None:
    # All stats in a single round-trip instead of 8 sequential queries
    try:
        row = await pool.fetchrow(
            """SELECT
                (SELECT COUNT(*)           FROM managed_bots)                                      AS total_bots,
                (SELECT COUNT(*)           FROM subscriptions WHERE is_active=true AND expires_at > now()) AS total_subs,
                (SELECT COUNT(*)           FROM payments WHERE status='confirmed')                 AS total_payments,
                (SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='confirmed')       AS revenue,
                (SELECT COUNT(*)           FROM platform_users)                                    AS total_users,
                (SELECT COUNT(*)           FROM platform_users
                    WHERE COALESCE(registered_at, first_seen) >= CURRENT_DATE)                     AS today_users,
                (SELECT COUNT(*)           FROM error_reports WHERE status='new')                  AS new_error_reports"""
        )
    except Exception:
        row = None
        log_exc_swallow(log, "_show_admin_main stats query failed")

    total_bots       = int(row["total_bots"]        or 0) if row else 0
    total_subs       = int(row["total_subs"]        or 0) if row else 0
    total_payments   = int(row["total_payments"]    or 0) if row else 0
    revenue          = float(row["revenue"]         or 0) if row else 0.0
    total_users      = int(row["total_users"]       or 0) if row else 0
    today_users      = int(row["today_users"]       or 0) if row else 0
    new_error_reports= int(row["new_error_reports"] or 0) if row else 0

    text = (
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b> (+{today_users} сегодня)\n"
        f"🤖 Ботов в системе: <b>{total_bots}</b>\n"
        f"💳 Активных подписок: <b>{total_subs}</b>\n"
        f"✅ Оплат подтверждено: <b>{total_payments}</b>\n"
        f"💰 Выручка (USD): <b>${float(revenue):.2f}</b>\n\n"
        f"📅 {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
    )
    kb = _admin_main_kb(new_error_reports=int(new_error_reports))
    if edit and hasattr(msg_or_cb, "message"):
        try:
            await msg_or_cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
    else:
        target = msg_or_cb if hasattr(msg_or_cb, "answer") else msg_or_cb.message
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Callback dispatcher ────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(
    callback: CallbackQuery, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа.", show_alert=True)
        return
    await callback.answer()
    action = callback.data.removeprefix("adm:")

    if action == "main":
        await _show_admin_main(callback, pool, edit=True)

    elif action == "section_users":
        await _adm_section_users(callback, pool)

    elif action == "section_billing":
        await _adm_section_billing(callback, pool)

    elif action == "section_assets":
        await _adm_section_assets(callback, pool)

    elif action == "section_ops":
        await _adm_section_ops(callback, pool)

    elif action == "section_ai":
        await _adm_section_ai(callback, pool)

    elif action == "section_system":
        await _adm_section_system(callback, pool)

    elif action == "users":
        await _adm_users(callback, pool)

    elif action == "subs":
        await _adm_subscriptions(callback, pool)

    elif action == "bots":
        await _adm_bots_summary(callback, pool)

    elif action == "stats":
        await _adm_system_stats(callback, pool)

    elif action == "broadcast":
        await callback.message.edit_text(
            "📨 <b>Рассылка по всем пользователям платформы</b>\n\n"
            "Отправьте текст сообщения следующим сообщением.\n"
            "Сообщение получат все зарегистрированные пользователи.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        # set FSM flag via message text — simple approach: store in temp table
        await pool.execute(
            "INSERT INTO admin_state(admin_id, state, data) "
            "VALUES($1,'broadcast','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='broadcast',data=''",
            callback.from_user.id,
        )

    elif action == "notify_toggle":
        global _NOTIFY_NEW_USERS
        _NOTIFY_NEW_USERS = not _NOTIFY_NEW_USERS
        await _show_admin_main(callback, pool, edit=True)

    elif action == "free_mode_toggle":
        new_state = not get_free_mode()
        set_free_mode(new_state)
        await db.set_platform_setting(
            pool, "free_mode", "true" if new_state else "false"
        )
        await _show_admin_main(callback, pool, edit=True)

    elif action == "block_ask":
        await callback.message.edit_text(
            "🚫 <b>Заблокировать пользователя</b>\n\nОтправьте Telegram ID (число):",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'block','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='block',data=''",
            callback.from_user.id,
        )

    elif action == "unblock_ask":
        await callback.message.edit_text(
            "✅ <b>Разблокировать пользователя</b>\n\nОтправьте Telegram ID:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'unblock','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='unblock',data=''",
            callback.from_user.id,
        )

    elif action == "delete_ask":
        await callback.message.edit_text(
            "🗑 <b>Удалить все данные пользователя</b>\n\n"
            "⚠️ Это действие необратимо! Отправьте Telegram ID:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'delete_user','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='delete_user',data=''",
            callback.from_user.id,
        )

    elif action == "grant_ask":
        await callback.message.edit_text(
            "💰 <b>Выдать подписку</b>\n\n"
            "Отправьте в формате:\n"
            "<code>USER_ID план месяцев</code>\n\n"
            "Пример: <code>123456789 pro 3</code>\n"
            "Планы: starter, pro, enterprise",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'grant','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='grant',data=''",
            callback.from_user.id,
        )

    elif action == "revoke_ask":
        await callback.message.edit_text(
            "❌ <b>Забрать подписку</b>\n\n"
            "Отправьте Telegram ID пользователя:\n"
            "<code>USER_ID</code>\n\n"
            "Пример: <code>123456789</code>\n\n"
            "Подписка будет деактивирована, пользователь вернётся на FREE.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'revoke','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='revoke',data=''",
            callback.from_user.id,
        )

    elif action == "tokens_file":
        await _adm_send_tokens_file(callback, pool)

    elif action == "users_csv":
        await _adm_send_users_csv(callback, pool)

    elif action == "find_user":
        await callback.message.edit_text(
            "🔍 <b>Поиск пользователя</b>\n\nОтправьте Telegram ID:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'find','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='find',data=''",
            callback.from_user.id,
        )

    elif action == "prices":
        await _adm_prices(callback)

    elif action.startswith("price_edit:"):
        plan = action.split(":", 1)[1]
        await _adm_price_edit_ask(callback, pool, plan)

    elif action == "pay_cfg":
        from bot.handlers.subscription import (
            _payment_settings_text,
            _payment_settings_kb,
        )

        await callback.message.edit_text(
            _payment_settings_text(),
            parse_mode="HTML",
            reply_markup=_payment_settings_kb(),
        )

    elif action == "swarm_mode":
        await _adm_swarm_mode(callback, pool)

    elif action == "ai_status":
        await _adm_ai_status(callback, pool)

    elif action == "env_list":
        await _adm_env_list(callback, http)

    elif action.startswith("env_edit:"):
        key = action.split(":", 1)[1]
        await _adm_env_edit_ask(callback, pool, key)

    elif action == "env_add":
        await callback.message.edit_text(
            "➕ <b>Добавить переменную</b>\n\n"
            "Отправьте в формате:\n<code>КЛЮЧ значение</code>\n\n"
            "Пример: <code>MY_VAR hello123</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'env_add','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='env_add',data=''",
            callback.from_user.id,
        )

    elif action.startswith("env_del:"):
        key = action.split(":", 1)[1]
        await _adm_env_delete(callback, http, key)

    elif action.startswith("set_mode:"):
        mode = action.split(":", 1)[1]
        await db.set_system_mode(pool, mode)
        await _adm_swarm_mode(callback, pool)

    elif action == "strike_grant_ask":
        await callback.message.edit_text(
            "⚔️ <b>Выдать Strike доступ</b>\n\n"
            "Отправьте Telegram ID пользователя:\n"
            "<code>USER_ID</code>\n\n"
            "Пример: <code>123456789</code>\n\n"
            "Доступ будет активирован немедленно.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'strike_grant','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='strike_grant',data=''",
            callback.from_user.id,
        )

    elif action == "strike_revoke_ask":
        await callback.message.edit_text(
            "⚔️ <b>Забрать Strike доступ</b>\n\n"
            "Отправьте Telegram ID пользователя:\n"
            "<code>USER_ID</code>\n\n"
            "Пример: <code>123456789</code>\n\n"
            "Strike доступ будет немедленно отозван.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'strike_revoke','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='strike_revoke',data=''",
            callback.from_user.id,
        )

    elif action == "bulk_grant_ask":
        await callback.message.edit_text(
            "💰 <b>Массовая выдача подписок</b>\n\n"
            "Отправьте список пользователей и план:\n\n"
            "<code>USER_ID план месяцев</code> — по одному на строку\n\n"
            "Пример:\n"
            "<code>123456 pro 3\n789012 starter 1\n345678 enterprise 6</code>\n\n"
            "Планы: <code>starter</code>, <code>pro</code>, <code>enterprise</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'bulk_grant','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='bulk_grant',data=''",
            callback.from_user.id,
        )

    elif action == "logs":
        await _adm_logs(callback, pool, source="ui", status_filter=None, page=0)

    elif action == "logs_err":
        await _adm_logs(callback, pool, source="ui", status_filter="error", page=0)

    elif action == "logs_ops":
        await _adm_logs(callback, pool, source="ops", status_filter=None, page=0)

    elif action == "logs_ops_err":
        await _adm_logs(callback, pool, source="ops", status_filter="error", page=0)

    elif action.startswith("logs_p:"):
        # logs_p:ui:none:0  or  logs_p:ops:error:1
        parts = action.split(":")
        if len(parts) == 4:
            src = parts[1]
            sf = parts[2] if parts[2] != "none" else None
            pg = int(parts[3])
            await _adm_logs(callback, pool, source=src, status_filter=sf, page=pg)

    elif action.startswith("logs_uid:"):
        uid_str = action.split(":", 1)[1]
        try:
            target_uid = int(uid_str)
        except ValueError:
            await callback.answer("Неверный ID", show_alert=True)
            return
        await _adm_logs(callback, pool, source="ui", status_filter=None, page=0, owner_filter=target_uid)

    elif action == "logs_find_user":
        await callback.message.edit_text(
            "🔍 <b>Логи по пользователю</b>\n\nВведите Telegram ID пользователя:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        try:
            await pool.execute(
                "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'logs_find_user','') "
                "ON CONFLICT(admin_id) DO UPDATE SET state='logs_find_user',data=''",
                callback.from_user.id,
            )
        except Exception:
            log_exc_swallow(log, "admin_state insert failed for logs_find_user")

    elif action == "platform_ops":
        await _adm_platform_ops(callback, pool)

    elif action == "cleanup_ask":
        await callback.message.edit_text(
            "🧹 <b>Очистка устаревших данных</b>\n\n"
            "Это удалит старые записи, освободив место в БД:\n"
            "• Лог флудов старше 30 дней\n"
            "• Завершённые операции старше 7 дней\n"
            "• Аудит операций старше 30 дней\n\n"
            "⚠️ <b>Действие необратимо!</b>\n\n"
            "Введите <code>CLEAN</code> для подтверждения:",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'cleanup','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='cleanup',data=''",
            callback.from_user.id,
        )

    elif action == "error_reports":
        await _adm_error_reports(callback, pool, page=0, status="new")

    elif action.startswith("error_reports:"):
        # Формат: error_reports:page:status  или  error_report:ID
        parts = action.split(":")
        if len(parts) == 3:
            page = int(parts[1])
            status = parts[2]
            await _adm_error_reports(callback, pool, page=page, status=status)
        else:
            await callback.message.edit_text(
                "❌ Некорректный формат.", reply_markup=_back_kb()
            )

    elif action.startswith("error_report:"):
        parts = action.split(":")
        report_id = int(parts[1])
        await _adm_show_error_report(callback, pool, report_id)

    elif action.startswith("err_status:"):
        # Формат: err_status:ID:new_status
        parts = action.split(":")
        if len(parts) >= 3:
            report_id = int(parts[1])
            new_status = parts[2]
            await _adm_set_error_report_status(callback, pool, report_id, new_status)
        else:
            await callback.message.edit_text(
                "❌ Некорректный формат.", reply_markup=_back_kb()
            )

    elif action == "audit_log":
        try:
            rows = await pool.fetch(
                """SELECT occurred_at, owner_id, action, target, result, error_msg
                   FROM operation_audit
                   ORDER BY occurred_at DESC LIMIT 25"""
            )
        except Exception:
            rows = []
        if rows:
            lines = []
            for r in rows:
                dt = (
                    r["occurred_at"].strftime("%d.%m %H:%M")
                    if r.get("occurred_at")
                    else "?"
                )
                uid = r.get("owner_id") or "?"
                act = r.get("action") or "?"
                res = r.get("result") or "?"
                tgt = r.get("target") or ""
                tgt_str = f" → {tgt[:20]}" if tgt else ""
                res_emoji = (
                    "✅" if res == "success" else ("⚠️" if res == "flood_wait" else "❌")
                )
                lines.append(f"<code>{dt}</code> uid:{uid} {act}{tgt_str} {res_emoji}")
            text = "🔐 <b>Аудит операций (последние 25)</b>\n\n" + "\n".join(lines)
        else:
            text = "🔐 <b>Аудит операций</b>\n\nЗаписей нет."
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="adm:users")
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )

    elif action == "exit":
        await callback.message.edit_text(
            "👋 Вышли из админки.",
            reply_markup=main_menu(is_admin=True),
        )


# ── Sub-screens ───────────────────────────────────────────────────────────────


async def _fetchval_or_zero(pool: asyncpg.Pool, query: str) -> int:
    try:
        return int(await pool.fetchval(query) or 0)
    except Exception:
        log_exc_swallow(log, "Admin dashboard metric query failed")
        return 0


async def _new_error_report_count(pool: asyncpg.Pool) -> int:
    return await _fetchval_or_zero(
        pool, "SELECT COUNT(*) FROM error_reports WHERE status='new'"
    )


def _provider_status_line(name: str, key_name: str, model_name: str) -> str:
    key_ok = bool(os.getenv(key_name))
    model = os.getenv(model_name, "auto")
    status = "✅" if key_ok else "❌"
    return f"{status} <b>{name}</b> · model: <code>{_html.escape(model)}</code>"


async def _adm_section_users(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    try:
        row = await pool.fetchrow(
            """SELECT
                (SELECT COUNT(*) FROM platform_users)                                              AS total,
                (SELECT COUNT(*) FROM platform_users
                    WHERE COALESCE(registered_at, first_seen) >= CURRENT_DATE)                     AS today,
                (SELECT COUNT(*) FROM platform_users WHERE COALESCE(is_banned,false)=true)        AS banned,
                (SELECT COUNT(*) FROM subscriptions WHERE is_active=true AND expires_at > now())   AS subscribers"""
        )
    except Exception:
        row = None
        log_exc_swallow(log, "_adm_section_users stats query failed")
    total       = int(row["total"]       or 0) if row else 0
    today       = int(row["today"]       or 0) if row else 0
    banned      = int(row["banned"]      or 0) if row else 0
    subscribers = int(row["subscribers"] or 0) if row else 0
    text = (
        "👥 <b>Пользователи</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"Новых сегодня: <b>{today}</b>\n"
        f"Активных подписок: <b>{subscribers}</b>\n"
        f"Заблокировано: <b>{banned}</b>\n\n"
        "Действия сгруппированы: просмотр, поиск, доступ, экспорт."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_admin_section_kb("users")
    )


async def _adm_section_billing(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    try:
        row = await pool.fetchrow(
            """SELECT
                (SELECT COUNT(*) FROM subscriptions WHERE is_active=true AND expires_at > now())  AS active,
                (SELECT COUNT(*) FROM payments WHERE status='confirmed')                           AS confirmed,
                (SELECT COUNT(*) FROM payments WHERE status='pending')                             AS pending,
                (SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='confirmed')       AS revenue"""
        )
    except Exception:
        row = None
        log_exc_swallow(log, "_adm_section_billing stats query failed")
    active    = int(row["active"]    or 0)   if row else 0
    confirmed = int(row["confirmed"] or 0)   if row else 0
    pending   = int(row["pending"]   or 0)   if row else 0
    revenue   = float(row["revenue"] or 0.0) if row else 0.0
    text = (
        "💳 <b>Деньги и подписки</b>\n\n"
        f"Активные подписки: <b>{active}</b>\n"
        f"Подтверждённых оплат: <b>{confirmed}</b>\n"
        f"Ожидают оплаты: <b>{pending}</b>\n"
        f"Выручка: <b>${revenue:.2f}</b>\n\n"
        "Здесь выдача, отзыв, bulk-выдача, цены и платёжные методы."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_admin_section_kb("billing")
    )


async def _adm_section_assets(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    try:
        row = await pool.fetchrow(
            """SELECT
                (SELECT COUNT(*) FROM managed_bots)                                                   AS bots,
                (SELECT COUNT(*) FROM managed_channels)                                               AS channels,
                (SELECT COUNT(*) FROM tg_accounts WHERE COALESCE(is_active,true)=true)               AS accounts,
                (SELECT COUNT(*) FROM platform_users WHERE COALESCE(strike_access,false)=true)        AS strike_users"""
        )
    except Exception:
        row = None
        log_exc_swallow(log, "_adm_section_assets stats query failed")
    bots         = int(row["bots"]         or 0) if row else 0
    channels     = int(row["channels"]     or 0) if row else 0
    accounts     = int(row["accounts"]     or 0) if row else 0
    strike_users = int(row["strike_users"] or 0) if row else 0
    text = (
        "🤖 <b>Боты, токены и Strike</b>\n\n"
        f"Ботов в системе: <b>{bots}</b>\n"
        f"Каналов/чатов: <b>{channels}</b>\n"
        f"Активных TG-аккаунтов: <b>{accounts}</b>\n"
        f"Strike-доступов: <b>{strike_users}</b>\n\n"
        "Здесь токены, рассылка и выдача доступа к Strike."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_admin_section_kb("assets")
    )


async def _adm_section_ops(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    try:
        row = await pool.fetchrow(
            """SELECT
                (SELECT COUNT(*) FROM operation_queue WHERE status='running')                                           AS running,
                (SELECT COUNT(*) FROM operation_queue WHERE status='pending')                                           AS pending,
                (SELECT COUNT(*) FROM operation_queue
                    WHERE status='failed' AND finished_at > now() - INTERVAL '24 hours')                               AS failed,
                (SELECT COUNT(*) FROM account_flood_log WHERE created_at > now() - INTERVAL '24 hours')               AS floods"""
        )
    except Exception:
        row = None
        log_exc_swallow(log, "_adm_section_ops stats query failed")
    running = int(row["running"] or 0) if row else 0
    pending = int(row["pending"] or 0) if row else 0
    failed  = int(row["failed"]  or 0) if row else 0
    floods  = int(row["floods"]  or 0) if row else 0
    text = (
        "⚙️ <b>Операции и здоровье процессов</b>\n\n"
        f"В работе: <b>{running}</b>\n"
        f"В очереди: <b>{pending}</b>\n"
        f"Ошибок за 24ч: <b>{failed}</b>\n"
        f"Flood-событий за 24ч: <b>{floods}</b>\n\n"
        "Смотри очередь, аудит, статистику и чистку данных."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_admin_section_kb("ops")
    )


async def _adm_section_ai(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    mode = await db.get_system_mode(pool)
    order = os.getenv("AI_PROVIDER_ORDER", "openrouter,gemini,groq")
    lines = [
        "🧠 <b>AI / провайдеры</b>",
        "",
        _provider_status_line("OpenRouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
        _provider_status_line("Gemini", "GEMINI_API_KEY", "GEMINI_MODEL"),
        _provider_status_line("Groq", "GROQ_API_KEY", "GROQ_MODEL"),
        f"🔁 Порядок: <code>{_html.escape(order)}</code>",
        f"⚙️ Swarm: <b>{_html.escape(mode.upper())}</b>",
        "",
        "Отсюда правим ключи, модели и режим работы системы.",
    ]
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_admin_section_kb("ai"),
    )


async def _adm_ai_status(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Live ping each configured AI provider and report latency/status."""
    import time as _time
    from services.ai_providers import configured_providers

    await callback.message.edit_text(
        "🧠 <b>Проверяю AI провайдеры...</b>\n\n⏳ Тестирую соединение с каждым...",
        parse_mode="HTML",
    )

    providers = configured_providers()
    if not providers:
        await callback.message.edit_text(
            "🧠 <b>Статус AI</b>\n\n❌ Нет настроенных провайдеров.\n\n"
            "Добавьте API ключи через 🔑 Переменные AI.",
            parse_mode="HTML",
            reply_markup=_admin_section_kb("ai"),
        )
        return

    async def _ping_one(provider) -> tuple[str, bool, int]:
        t0 = _time.monotonic()
        try:
            payload = {
                "model": provider.models[0],
                "messages": [{"role": "user", "content": "1+1=?"}],
                "max_tokens": 5,
            }
            headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{provider.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=8),
                    ssl=False,
                ) as resp:
                    ok = resp.status < 500
                    ms = int((_time.monotonic() - t0) * 1000)
                    return provider.name, ok, ms
        except Exception:
            ms = int((_time.monotonic() - t0) * 1000)
            return provider.name, False, ms

    results = await asyncio.gather(*[_ping_one(p) for p in providers], return_exceptions=True)

    lines = ["🧠 <b>Статус AI провайдеров (live)</b>", ""]
    for r in results:
        if isinstance(r, BaseException):
            lines.append(f"❓ Неизвестная ошибка: {r}")
            continue
        name, ok, ms = r
        icon = "✅" if ok else "❌"
        lines.append(f"{icon} <b>{name}</b> · {ms} мс")

    lines.extend(["", "<i>Обновлено сейчас</i>"])
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="adm:ai_status")
    kb.button(text="◀️ Назад", callback_data="adm:section_ai")
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _adm_section_system(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    mode, new_errors = await asyncio.gather(
        db.get_system_mode(pool),
        _new_error_report_count(pool),
        return_exceptions=True,
    )
    if isinstance(mode, BaseException):
        mode = "auto"
    if isinstance(new_errors, BaseException):
        new_errors = 0
    env_flags = {
        "Railway": bool(os.getenv("RAILWAY_TOKEN")),
        "TG API": bool(os.getenv("TG_API_ID") and os.getenv("TG_API_HASH")),
        "Manager token": bool(os.getenv("MANAGER_BOT_TOKEN")),
        "Admins": bool(os.getenv("ADMIN_IDS")),
    }
    env_lines = "\n".join(
        f"{'✅' if ok else '❌'} {name}" for name, ok in env_flags.items()
    )
    text = (
        "🛠 <b>Система</b>\n\n"
        f"Free Mode: <b>{'ВКЛ' if get_free_mode() else 'ВЫКЛ'}</b>\n"
        f"Уведомления о новых: <b>{'ВКЛ' if _NOTIFY_NEW_USERS else 'ВЫКЛ'}</b>\n"
        f"Swarm режим: <b>{_html.escape(mode.upper())}</b>\n"
        f"Новых отчётов об ошибках: <b>{new_errors}</b>\n\n"
        f"{env_lines}"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_admin_section_kb("system", new_error_reports=new_errors),
    )


async def _adm_users(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    from bot.handlers.admin_users import AdminUserCb

    _PLAN_EMO = {"free": "🆓", "starter": "⭐", "pro": "🚀", "enterprise": "👑"}
    try:
        rows, total_row = await asyncio.gather(
            pool.fetch(
                """SELECT pu.user_id, pu.username, pu.first_name,
                          COALESCE(pu.current_plan, 'free') as current_plan,
                          COALESCE(pu.is_banned, false) as is_banned,
                          s.plan as sub_plan, s.expires_at as sub_exp
                   FROM platform_users pu
                   LEFT JOIN subscriptions s
                     ON s.user_id=pu.user_id AND s.is_active=true AND s.expires_at > now()
                   ORDER BY COALESCE(pu.registered_at, pu.first_seen) DESC NULLS LAST
                   LIMIT 15"""
            ),
            pool.fetchval("SELECT COUNT(*) FROM platform_users"),
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ <code>{e}</code>", parse_mode="HTML", reply_markup=_back_kb()
        )
        return
    total = total_row or 0
    kb = InlineKeyboardBuilder()
    lines = []
    for r in rows:
        plan = r["sub_plan"] or r["current_plan"] or "free"
        emo = _PLAN_EMO.get(plan, "❓")
        ban = "🚫 " if r["is_banned"] else ""
        raw_name = (
            f"@{r['username']}"
            if r["username"]
            else r["first_name"] or f"#{r['user_id']}"
        )
        name = _html.escape(raw_name)
        exp = ""
        if r["sub_exp"]:
            exp = f" до {r['sub_exp'].strftime('%d.%m')}"
        lines.append(f"{ban}{emo} {name} — {plan.upper()}{exp}")
        kb.button(
            text=f"{ban}{emo} {name[:22]}",
            callback_data=AdminUserCb(action="user_actions", user_id=r["user_id"]),
        )
    body = "\n".join(lines) if lines else "Нет зарегистрированных пользователей."
    kb.button(text="📋 Полный список", callback_data=AdminUserCb(action="list"))
    kb.button(text="📥 Экспорт CSV", callback_data=AdminUserCb(action="export_csv"))
    kb.button(text="◀️ Назад", callback_data="adm:main")
    kb.adjust(1)
    await callback.message.edit_text(
        f"👥 <b>Пользователи платформы</b> (всего: <b>{total}</b>)\n\n{body}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _adm_subscriptions(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    active = await pool.fetch(
        "SELECT user_id, plan, expires_at FROM subscriptions "
        "WHERE is_active=true AND expires_at > now() ORDER BY expires_at DESC LIMIT 20"
    )
    expired = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE is_active=false OR expires_at <= now()"
        )
        or 0
    )
    lines = []
    for s in active:
        lines.append(
            f"<code>{s['user_id']}</code> — <b>{s['plan'].upper()}</b> "
            f"до {s['expires_at'].strftime('%d.%m.%Y')}"
        )
    body = "\n".join(lines) if lines else "Активных подписок нет."
    await callback.message.edit_text(
        f"💳 <b>Активные подписки</b>\n\n{body}\n\n<i>Истёкших: {expired}</i>",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )


async def _adm_bots_summary(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name, added_by, added_at "
        "FROM managed_bots ORDER BY added_at DESC LIMIT 20"
    )
    lines = []
    for b in bots:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        lines.append(
            f"<code>{b['bot_id']}</code> {label} (owner: <code>{b['added_by']}</code>)"
        )
    body = "\n".join(lines) if lines else "Ботов нет."
    await callback.message.edit_text(
        f"🤖 <b>Последние 20 ботов в системе</b>\n\n{body}\n\n"
        "Для полного списка с токенами нажмите «Экспорт токенов».",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )


async def _adm_system_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    total_msgs = (
        await pool.fetchval("SELECT COALESCE(SUM(sent_count),0) FROM broadcasts") or 0
    )
    total_bc = await pool.fetchval("SELECT COUNT(*) FROM broadcasts") or 0
    total_relay = await pool.fetchval("SELECT COUNT(*) FROM relay_sessions") or 0
    total_funnels = await pool.fetchval("SELECT COUNT(*) FROM funnels") or 0
    total_schedules = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM scheduled_broadcasts WHERE status='pending'"
        )
        or 0
    )
    db_users = await pool.fetchval("SELECT COUNT(*) FROM bot_users") or 0
    mode = await db.get_system_mode(pool)

    await callback.message.edit_text(
        "📊 <b>Системная статистика</b>\n\n"
        f"💬 Сообщений отправлено: <b>{int(total_msgs):,}</b>\n"
        f"📢 Рассылок всего: <b>{int(total_bc):,}</b>\n"
        f"📨 Relay-диалогов: <b>{int(total_relay):,}</b>\n"
        f"🔗 Цепочек: <b>{int(total_funnels):,}</b>\n"
        f"⏰ Запланировано: <b>{int(total_schedules):,}</b>\n"
        f"👥 Записей в bot_users: <b>{int(db_users):,}</b>\n\n"
        f"🌐 Swarm mode: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=_back_kb(),
    )


async def _adm_send_tokens_file(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name, token, added_by, added_at "
        "FROM managed_bots ORDER BY added_by, added_at"
    )
    lines = ["BOT_ID\tUSERNAME\tNAME\tOWNER_ID\tCREATED\tTOKEN"]
    for b in bots:
        label = b["username"] or b["first_name"] or "unknown"
        lines.append(
            f"{b['bot_id']}\t@{label}\t{b['first_name'] or ''}\t"
            f"{b['added_by']}\t{b['added_at'].strftime('%Y-%m-%d')}\t{b['token']}"
        )
    content = "\n".join(lines).encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    file = BufferedInputFile(content, filename=f"tokens_{ts}.tsv")
    await callback.message.answer_document(
        file,
        caption=f"🔑 Токены всех ботов ({len(bots)} шт.) — {ts} UTC\n"
        "<b>⚠️ Держите файл в тайне!</b>",
        parse_mode="HTML",
    )


async def _adm_send_users_csv(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        """SELECT mb.added_by, COUNT(DISTINCT mb.bot_id) as bots,
                  s.plan, s.expires_at
           FROM managed_bots mb
           LEFT JOIN subscriptions s ON s.user_id=mb.added_by AND s.is_active=true
           GROUP BY mb.added_by, s.plan, s.expires_at
           ORDER BY mb.added_by"""
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "bots_count", "plan", "expires_at"])
    for r in rows:
        writer.writerow(
            [
                r["added_by"],
                r["bots"],
                r["plan"] or "free",
                r["expires_at"].strftime("%Y-%m-%d") if r["expires_at"] else "",
            ]
        )
    content = buf.getvalue().encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    file = BufferedInputFile(content, filename=f"users_{ts}.csv")
    await callback.message.answer_document(
        file,
        caption=f"📋 Экспорт пользователей ({len(rows)} чел.) — {ts} UTC",
    )


async def _adm_prices(callback: CallbackQuery) -> None:
    import config

    kb = InlineKeyboardBuilder()
    for plan, price in config.PLAN_PRICES_USD.items():
        emo = {"starter": "⭐", "pro": "🚀", "enterprise": "👑"}.get(plan, "")
        kb.button(
            text=f"✏️ {emo} {plan.upper()} — ${price}/мес",
            callback_data=f"adm:price_edit:{plan}",
        )
    kb.button(text="◀️ Назад", callback_data="adm:main")
    kb.adjust(1)
    s = config.PLAN_PRICES_USD
    await callback.message.edit_text(
        "💰 <b>Цены на подписки</b>\n\n"
        f"⭐ STARTER — <b>${s['starter']}/мес</b>\n"
        f"🚀 PRO — <b>${s['pro']}/мес</b>\n"
        f"👑 ENTERPRISE — <b>${s['enterprise']}/мес</b>\n\n"
        "Нажмите на план чтобы изменить цену.\n"
        "Новая цена применится сразу и сохранится в Railway.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _adm_price_edit_ask(
    callback: CallbackQuery, pool: asyncpg.Pool, plan: str
) -> None:
    import config

    emo = {"starter": "⭐", "pro": "🚀", "enterprise": "👑"}.get(plan, "")
    cur = config.PLAN_PRICES_USD.get(plan, 0)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data="adm:prices")
    await callback.message.edit_text(
        f"✏️ <b>Цена {emo} {plan.upper()}</b>\n\n"
        f"Текущая цена: <b>${cur}/мес</b>\n\n"
        "Отправьте новую цену в USD (только число, например <code>15</code>):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await pool.execute(
        "INSERT INTO admin_state(admin_id,state,data) VALUES($1,$2,'') "
        "ON CONFLICT(admin_id) DO UPDATE SET state=$2,data=''",
        callback.from_user.id,
        f"price_edit:{plan}",
    )


_SWARM_MODE_DESCRIPTIONS = {
    "manual":     "🟢 Manual — вы запускаете каждую операцию вручную. Полный контроль, ничего автоматически.",
    "assisted":   "🟡 Assisted — система предлагает оптимизации, но вы подтверждаете. Рекомендуется для начала.",
    "autopilot":  "🔵 Autopilot — автоматически оптимизирует расписание, очередь и роутинг операций.",
    "growth":     "🔴 Growth — агрессивный рост: максимальная скорость операций, больше аккаунтов в параллели.",
    "experiment": "🟣 Experiment — максимальное A/B тестирование, пробует новые стратегии роутинга.",
    "stability":  "⚫ Stability — фиксированный роутинг без изменений, приоритет надёжности над скоростью.",
}

async def _adm_swarm_mode(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    current = await db.get_system_mode(pool)
    kb = InlineKeyboardBuilder()
    for mode, desc in _SWARM_MODE_DESCRIPTIONS.items():
        prefix = "✅ " if mode == current else ""
        short_label = desc.split("—")[0].strip()
        kb.button(text=f"{prefix}{short_label}", callback_data=f"adm:set_mode:{mode}")
    kb.button(text="◀️ Назад", callback_data="adm:section_ai")
    kb.adjust(1)

    current_desc = _SWARM_MODE_DESCRIPTIONS.get(current, current)
    desc_lines = "\n".join(f"  {d}" for d in _SWARM_MODE_DESCRIPTIONS.values())
    await callback.message.edit_text(
        f"⚙️ <b>Swarm режим</b>\n\n"
        f"Текущий: <b>{current.upper()}</b>\n"
        f"<i>{current_desc.split('—',1)[-1].strip()}</i>\n\n"
        f"<b>Описание режимов:</b>\n{desc_lines}\n\n"
        "Выберите режим работы системы:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Message handler for admin FSM states ─────────────────────────────────────


@router.message(F.text)
async def handle_admin_message(
    message: Message, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    if not _is_admin(message.from_user.id):
        return

    # Check admin state
    try:
        state_row = await pool.fetchrow(
            "SELECT state, data FROM admin_state WHERE admin_id=$1",
            message.from_user.id,
        )
    except Exception:
        return  # admin_state table not yet created

    if not state_row:
        return

    state = state_row["state"]
    text = message.text.strip()

    await pool.execute(
        "DELETE FROM admin_state WHERE admin_id=$1", message.from_user.id
    )

    if state == "broadcast":
        users = await pool.fetch("SELECT DISTINCT added_by FROM managed_bots")
        sent = 0
        for u in users:
            try:
                await message.bot.send_message(u["added_by"], text)
                sent += 1
                await asyncio.sleep(0.3)
            except Exception:
                log_exc_swallow(
                    log,
                    "Не удалось отправить сообщение рассылки пользователю",
                    user_id=u["added_by"],
                )
        await message.answer(
            f"✅ Рассылка завершена\n\nОтправлено: <b>{sent}</b> / {len(users)}",
            parse_mode="HTML",
            reply_markup=_admin_main_kb(),
        )

    elif state == "block":
        try:
            uid = int(text)
            await pool.execute(
                "INSERT INTO blocked_users(user_id) VALUES($1) ON CONFLICT DO NOTHING",
                uid,
            )
            await message.answer(
                f"🚫 Пользователь <code>{uid}</code> заблокирован.",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "unblock":
        try:
            uid = int(text)
            await pool.execute("DELETE FROM blocked_users WHERE user_id=$1", uid)
            await message.answer(
                f"✅ Пользователь <code>{uid}</code> разблокирован.",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "delete_user":
        try:
            uid = int(text)
            bot_ids = await pool.fetch(
                "SELECT bot_id FROM managed_bots WHERE added_by=$1", uid
            )
            for b in bot_ids:
                await pool.execute(
                    "DELETE FROM managed_bots WHERE bot_id=$1", b["bot_id"]
                )
            await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", uid)
            await pool.execute("DELETE FROM payments WHERE user_id=$1", uid)
            await message.answer(
                f"🗑 Данные пользователя <code>{uid}</code> удалены "
                f"({len(bot_ids)} ботов).",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "grant":
        try:
            parts = text.split()
            uid = int(parts[0])
            plan = parts[1].lower()
            months = int(parts[2]) if len(parts) > 2 else 1
            months = max(1, min(months, 1200))  # cap: 1–1200 месяцев (100 лет)
            if plan not in ("starter", "pro", "enterprise"):
                raise ValueError("bad plan")
            await pool.execute(
                """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
                   VALUES($1, $2, now() + ($3 || ' months')::INTERVAL, true)
                   ON CONFLICT(user_id) DO UPDATE
                   SET plan      = EXCLUDED.plan,
                       is_active = true,
                       expires_at = CASE
                           WHEN subscriptions.expires_at > now()
                               THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
                           ELSE now() + ($3 || ' months')::INTERVAL
                       END""",
                uid,
                plan,
                str(months),
            )
            row = await pool.fetchrow(
                "SELECT expires_at FROM subscriptions WHERE user_id=$1", uid
            )
            expires = row["expires_at"]
            await message.answer(
                f"✅ Подписка <b>{plan.upper()}</b> выдана пользователю "
                f"<code>{uid}</code> на {months} мес.",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
            try:
                await message.bot.send_message(
                    uid,
                    f"🎁 <b>Подарок!</b>\n\nВам активирована подписка "
                    f"<b>{plan.upper()}</b> на {months} месяц(ев).\n"
                    f"Действует до {expires.strftime('%d.%m.%Y')}.",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log,
                    "Не удалось уведомить пользователя о выдаче подписки",
                    user_id=uid,
                )
        except (ValueError, IndexError):
            await message.answer(
                "❌ Формат: <code>USER_ID план месяцев</code>\n"
                "Пример: <code>123456 pro 3</code>",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )

    elif state == "find":
        try:
            uid = int(text)
            bots = await pool.fetch(
                "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1",
                uid,
            )
            sub = await pool.fetchrow(
                "SELECT plan, expires_at FROM subscriptions "
                "WHERE user_id=$1 AND is_active=true AND expires_at > now()",
                uid,
            )
            plan_info = (
                f"{sub['plan'].upper()} до {sub['expires_at'].strftime('%d.%m.%Y')}"
                if sub
                else "FREE"
            )
            bot_lines = []
            for b in bots[:10]:
                label = f"@{b['username']}" if b["username"] else b["first_name"]
                bot_lines.append(f"  • {label} (<code>{b['bot_id']}</code>)")
            body = "\n".join(bot_lines) if bot_lines else "  Нет ботов"
            await message.answer(
                f"🔍 <b>Пользователь <code>{uid}</code></b>\n\n"
                f"Подписка: <b>{plan_info}</b>\n"
                f"Ботов: <b>{len(bots)}</b>\n\n"
                f"{body}",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "bulk_grant":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        ok_list, fail_list = [], []
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                fail_list.append(f"⚠️ Формат: {line[:30]}")
                continue
            try:
                uid = int(parts[0])
                plan = parts[1].lower()
                months = int(parts[2]) if len(parts) > 2 else 1
                months = max(1, min(months, 1200))
                if plan not in ("starter", "pro", "enterprise"):
                    raise ValueError("bad plan")
                await pool.execute(
                    """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
                       VALUES($1, $2, now() + ($3 || ' months')::INTERVAL, true)
                       ON CONFLICT(user_id) DO UPDATE
                       SET plan      = EXCLUDED.plan,
                           is_active = true,
                           expires_at = CASE
                               WHEN subscriptions.expires_at > now()
                                   THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
                               ELSE now() + ($3 || ' months')::INTERVAL
                           END""",
                    uid,
                    plan,
                    str(months),
                )
                ok_list.append(f"✅ {uid} → {plan.upper()} {months}м.")
                try:
                    await message.bot.send_message(
                        uid,
                        f"🎁 <b>Подарок!</b> Вам активирована подписка <b>{plan.upper()}</b> "
                        f"на {months} мес.",
                        parse_mode="HTML",
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        "Не удалось уведомить пользователя о массовой выдаче подписки",
                        user_id=uid,
                    )
            except (ValueError, IndexError) as e:
                fail_list.append(f"❌ {line[:30]}: {e}")
        result_lines = ok_list[:20] + fail_list[:10]
        await message.answer(
            f"💰 <b>Массовая выдача завершена</b>\n\n"
            f"Успешно: <b>{len(ok_list)}</b>, ошибок: <b>{len(fail_list)}</b>\n\n"
            + "\n".join(result_lines),
            parse_mode="HTML",
            reply_markup=_admin_main_kb(),
        )

    elif state == "revoke":
        try:
            uid = int(text.strip())
            await db.revoke_plan_from_user(pool, uid, message.from_user.id)
            await message.answer(
                f"❌ Подписка отозвана у пользователя <code>{uid}</code>.\n"
                f"Пользователь переведён на план <b>FREE</b>.",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
            try:
                await message.bot.send_message(
                    uid,
                    "ℹ️ <b>Ваша подписка была отозвана администратором.</b>\n\n"
                    "Вы переведены на план FREE.\n"
                    "Для восстановления доступа оформите подписку: /menu → ⚙️ Настройки → 💳 Подписка",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log,
                    "Не удалось уведомить пользователя об отзыве подписки",
                    user_id=uid,
                )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "strike_grant":
        try:
            target_uid = int(text.strip())
            from bot.handlers.strike import _ensure_table

            await _ensure_table(pool)
            await pool.execute(
                "INSERT INTO strike_access (user_id, granted_by) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO NOTHING",
                target_uid,
                message.from_user.id,
            )
            await message.answer(
                f"⚔️ <b>Strike доступ активирован</b>\n\n"
                f"Пользователь <code>{target_uid}</code> теперь имеет доступ к Strike Module.",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
            try:
                await message.bot.send_message(
                    target_uid,
                    "⚔️ <b>Strike Module активирован!</b>\n\n"
                    "Администратор предоставил вам доступ к Strike Module.\n"
                    "Откройте меню для использования.",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log,
                    "Не удалось уведомить пользователя о выдаче Strike доступа",
                    user_id=target_uid,
                )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "strike_revoke":
        try:
            target_uid = int(text.strip())
            await db.revoke_strike_access(pool, target_uid, message.from_user.id)
            await message.answer(
                f"⚔️ <b>Strike доступ отозван</b>\n\n"
                f"У пользователя <code>{target_uid}</code> больше нет доступа к Strike Module.",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
            try:
                await message.bot.send_message(
                    target_uid,
                    "ℹ️ <b>Strike доступ был отозван администратором.</b>\n\n"
                    "Для получения доступа обратитесь к администратору.",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log,
                    "Не удалось уведомить пользователя об отзыве Strike доступа",
                    user_id=target_uid,
                )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "logs_find_user":
        try:
            target_uid = int(text.strip())
        except ValueError:
            await message.answer("❌ Неверный Telegram ID.", reply_markup=_admin_main_kb())
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="🖱 UI-события", callback_data=f"adm:logs_uid:{target_uid}")
        kb.button(text="⚙️ TG-операции", callback_data=f"adm:logs_p:ops:none:0")
        kb.button(text="◀️ Логи", callback_data="adm:logs")
        kb.adjust(2, 1)
        await message.answer(
            f"🔍 Показываю логи для uid <code>{target_uid}</code>:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif state == "cleanup":
        if text.strip().upper() != "CLEAN":
            await message.answer(
                "❌ Отменено (введите CLEAN для подтверждения).",
                reply_markup=_admin_main_kb(),
            )
            return
        try:
            flood_del = (
                await pool.fetchval(
                    "WITH d AS (DELETE FROM account_flood_log WHERE created_at < now() - INTERVAL '30 days' RETURNING 1) SELECT COUNT(*) FROM d"
                )
                or 0
            )
        except Exception:
            flood_del = 0
        try:
            ops_del = (
                await pool.fetchval(
                    "WITH d AS (DELETE FROM operation_queue WHERE status IN ('done','failed') "
                    "AND finished_at < now() - INTERVAL '7 days' RETURNING 1) SELECT COUNT(*) FROM d"
                )
                or 0
            )
        except Exception:
            ops_del = 0
        try:
            audit_del = (
                await pool.fetchval(
                    "WITH d AS (DELETE FROM operation_audit WHERE occurred_at < now() - INTERVAL '30 days' RETURNING 1) SELECT COUNT(*) FROM d"
                )
                or 0
            )
        except Exception:
            audit_del = 0
        try:
            dm_del = (
                await pool.fetchval(
                    "WITH d AS (DELETE FROM dm_campaign_log WHERE sent_at < now() - INTERVAL '90 days' RETURNING 1) SELECT COUNT(*) FROM d"
                )
                or 0
            )
        except Exception:
            dm_del = 0
        try:
            act_del = (
                await pool.fetchval(
                    "WITH d AS (DELETE FROM activity_log WHERE occurred_at < now() - INTERVAL '14 days' RETURNING 1) SELECT COUNT(*) FROM d"
                )
                or 0
            )
        except Exception:
            act_del = 0
        await message.answer(
            f"🧹 <b>Очистка завершена</b>\n\n"
            f"• Флуд-логов удалено: <b>{flood_del}</b>\n"
            f"• Операций удалено: <b>{ops_del}</b>\n"
            f"• Аудит-записей удалено: <b>{audit_del}</b>\n"
            f"• DM-логов удалено: <b>{dm_del}</b>\n"
            f"• Activity-логов удалено: <b>{act_del}</b>",
            parse_mode="HTML",
            reply_markup=_admin_main_kb(),
        )

    elif state.startswith("price_edit:"):
        plan = state.split(":", 1)[1]
        try:
            price = int(text.strip().replace("$", "").replace(" ", ""))
            if price < 1 or price > 9999:
                raise ValueError
            import config

            config.PLAN_PRICES_USD[plan] = price
            os.environ[f"PRICE_{plan.upper()}"] = str(price)
            try:
                async with aiohttp.ClientSession() as tmp:
                    await railway_api.set_variable(
                        tmp, f"PRICE_{plan.upper()}", str(price)
                    )
                note = "Сохранено в Railway."
            except Exception:
                note = "⚠️ Railway не настроен — цена активна до перезапуска."
            await message.answer(
                f"✅ Цена <b>{plan.upper()}</b> обновлена: <b>${price}/мес</b>\n\n{note}",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer(
                "❌ Введите целое число от 1 до 9999", reply_markup=_admin_main_kb()
            )

    elif state.startswith("env_edit:"):
        key = state.split(":", 1)[1]
        async with aiohttp.ClientSession() as tmp_http:
            try:
                await railway_api.set_variable(tmp_http, key, text)
                os.environ[key] = text  # update in-process immediately
                await message.answer(
                    f"✅ <b>Переменная обновлена</b>\n\n"
                    f"<code>{key}</code> = <code>{text[:80]}{'...' if len(text) > 80 else ''}</code>\n\n"
                    "Railway начнёт переразворачивание автоматически.",
                    parse_mode="HTML",
                    reply_markup=_admin_main_kb(),
                )
            except Exception as e:
                await message.answer(
                    f"❌ <b>Ошибка Railway API</b>\n\n<code>{e}</code>",
                    parse_mode="HTML",
                    reply_markup=_admin_main_kb(),
                )

    elif state == "env_add":
        parts = text.split(None, 1)
        if len(parts) != 2:
            await message.answer(
                "❌ Неверный формат. Нужно: <code>КЛЮЧ значение</code>",
                parse_mode="HTML",
                reply_markup=_admin_main_kb(),
            )
            return
        key, val = parts[0].upper(), parts[1]
        async with aiohttp.ClientSession() as tmp_http:
            try:
                await railway_api.set_variable(tmp_http, key, val)
                os.environ[key] = val
                await message.answer(
                    f"✅ <b>Переменная добавлена</b>\n\n"
                    f"<code>{key}</code> = <code>{val[:80]}{'...' if len(val) > 80 else ''}</code>\n\n"
                    "Railway начнёт переразворачивание автоматически.",
                    parse_mode="HTML",
                    reply_markup=_admin_main_kb(),
                )
            except Exception as e:
                await message.answer(
                    f"❌ <b>Ошибка Railway API</b>\n\n<code>{e}</code>",
                    parse_mode="HTML",
                    reply_markup=_admin_main_kb(),
                )


# ── Railway env var management helpers ────────────────────────────────────────


async def _adm_env_list(callback: CallbackQuery, http: aiohttp.ClientSession) -> None:
    if not railway_api.is_configured():
        await callback.message.edit_text(
            "🔑 <b>Переменные Railway</b>\n\n"
            "⚠️ Railway API не настроен.\n\n"
            "Добавьте <b>2 переменные</b> вручную в Railway Dashboard → Variables:\n\n"
            "1. <code>RAILWAY_TOKEN</code>\n"
            "   → railway.com/account → Tokens → Create Token\n\n"
            "2. <code>RAILWAY_PROJECT_ID</code>\n"
            "   → UUID из URL вашего проекта:\n"
            "   <code>railway.com/project/<b>ВОТ-ЭТОТ-UUID</b></code>\n\n"
            "Service ID и Environment ID определятся <b>автоматически</b>.\n\n"
            "После добавления этих 2 переменных — управление всеми остальными будет здесь.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    try:
        vars_online = await railway_api.list_variables(http)
    except Exception as e:
        await callback.message.edit_text(
            f"❌ <b>Ошибка Railway API</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    total = len(vars_online)
    await callback.message.edit_text(
        f"🔑 <b>Переменные окружения (Railway)</b>\n\n"
        f"Всего переменных в Railway: <b>{total}</b>\n\n"
        f"Нажмите на переменную чтобы изменить или удалить её.\n"
        f"➕ Добавить переменную — любой ключ и значение.\n\n"
        f"После изменения Railway автоматически перезапустит сервис (~1 мин).\n\n"
        f"✅ = задана   ❌ = не задана",
        parse_mode="HTML",
        reply_markup=_env_list_kb(vars_online),
    )


async def _adm_env_edit_ask(
    callback: CallbackQuery, pool: asyncpg.Pool, key: str
) -> None:
    label = next((l for k, l in _ENV_VARS if k == key), key)
    cur_val = os.getenv(key, "")
    masked = ""
    if cur_val:
        if len(cur_val) > 8:
            masked = cur_val[:4] + "****" + cur_val[-4:]
        else:
            masked = "****"

    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить переменную", callback_data=f"adm:env_del:{key}")
    kb.button(text="◀️ Назад", callback_data="adm:env_list")
    kb.adjust(1)

    # Special hint for ADMIN_IDS — show user's own ID
    extra_hint = ""
    if key == "ADMIN_IDS":
        uid = callback.from_user.id
        extra_hint = (
            f"\n💡 <b>Ваш Telegram ID:</b> <code>{uid}</code>\n"
            "Введите через запятую если нужно несколько: "
            f"<code>{uid},другой_id</code>\n"
            "После сохранения кнопка ⚙️ Админка появится в главном меню."
        )
    elif key == "RAILWAY_TOKEN":
        extra_hint = (
            "\n💡 Получить: railway.com → Account Settings → Tokens → Create Token"
        )
    elif key == "RAILWAY_PROJECT_ID":
        extra_hint = "\n💡 UUID из URL проекта: railway.com/project/<b>ВОТ-ЭТО</b>"
    elif key in ("TON_WALLET", "TRON_WALLET"):
        extra_hint = "\n💡 После сохранения кнопка оплаты появится в /subscription"

    await callback.message.edit_text(
        f"✏️ <b>{label}</b>\n\n"
        f"Ключ: <code>{key}</code>\n"
        f"Текущее значение: <code>{masked if masked else 'не задано'}</code>\n"
        f"{extra_hint}\n\n"
        "Отправьте новое значение следующим сообщением:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await pool.execute(
        "INSERT INTO admin_state(admin_id,state,data) VALUES($1,$2,'') "
        "ON CONFLICT(admin_id) DO UPDATE SET state=$2,data=''",
        callback.from_user.id,
        f"env_edit:{key}",
    )


async def _adm_env_delete(
    callback: CallbackQuery, http: aiohttp.ClientSession, key: str
) -> None:
    try:
        await railway_api.delete_variable(http, key)
        os.environ.pop(key, None)
        await _adm_env_list(callback, http)
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка удаления {_html.escape(key)}: {_html.escape(str(e))}",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )


# ── Platform operations analytics ────────────────────────────────────────────


async def _adm_platform_ops(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Платформенная аналитика по операциям всех пользователей."""
    try:
        total_ops = await pool.fetchval("SELECT COUNT(*) FROM operation_queue") or 0
        running = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='running'"
            )
            or 0
        )
        pending = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='pending'"
            )
            or 0
        )
        done_today = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='done' "
                "AND finished_at > now() - INTERVAL '24 hours'"
            )
            or 0
        )
        failed_today = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='failed' "
                "AND finished_at > now() - INTERVAL '24 hours'"
            )
            or 0
        )
        top_ops = await pool.fetch(
            """SELECT op_type, COUNT(*) AS cnt
               FROM operation_queue
               WHERE created_at > now() - INTERVAL '7 days'
               GROUP BY op_type ORDER BY cnt DESC LIMIT 5"""
        )
        total_floods = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM account_flood_log WHERE created_at > now() - INTERVAL '24 hours'"
            )
            or 0
        )
        active_accounts = (
            await pool.fetchval(
                "SELECT COUNT(DISTINCT owner_id) FROM tg_accounts WHERE is_active=true"
            )
            or 0
        )
        dm_sent = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM dm_campaign_log WHERE status='sent' "
                "AND sent_at > now() - INTERVAL '24 hours'"
            )
            or 0
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка получения данных: {e}",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    lines = [
        "📈 <b>Платформенная аналитика операций</b>\n",
        f"🔵 Активных операций: <b>{running}</b>",
        f"⏳ В очереди: <b>{pending}</b>",
        f"✅ Завершено за 24ч: <b>{done_today}</b>",
        f"❌ Ошибок за 24ч: <b>{failed_today}</b>",
        f"⚡ Всего операций: <b>{total_ops}</b>",
        "",
        f"📊 Флудов за 24ч: <b>{total_floods}</b>",
        f"👤 Активных владельцев: <b>{active_accounts}</b>",
        f"📨 DM-сообщений за 24ч: <b>{dm_sent}</b>",
    ]
    if top_ops:
        lines.append("\n🔝 <b>Топ операций (7 дней):</b>")
        for row in top_ops:
            lines.append(f"• {row['op_type']}: <b>{row['cnt']}</b>")

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb()
    )


# ── Error Reports Admin UI ────────────────────────────────────────────────────

_ERR_STATUS_LABELS: dict[str, str] = {
    "new": "🆕 Новые",
    "viewing": "👁 Просматриваются",
    "fixing": "🔧 В работе",
    "fixed": "✅ Исправлены",
    "duplicate": "🔄 Дубликаты",
}
_PAGE_SIZE = 8


def _error_reports_kb(
    reports: list,
    page: int,
    status: str,
    total: int,
) -> object:
    kb = InlineKeyboardBuilder()

    # Фильтр по статусу
    for st, label in _ERR_STATUS_LABELS.items():
        marker = "▶ " if st == status else ""
        kb.button(
            text=f"{marker}{label}", callback_data=f"adm:error_reports:{page}:{st}"
        )
    kb.adjust(3)

    # Список отчётов
    for r in reports:
        r["created_at"].strftime("%d.%m %H:%M") if r.get("created_at") else "?"
        user_label = f"@{r['username']}" if r.get("username") else f"id{r['user_id']}"
        desc_short = (r["description"] or "")[:28].replace("\n", " ")
        kb.button(
            text=f"#{r['id']} {user_label} — {desc_short}",
            callback_data=f"adm:error_report:{r['id']}",
        )
    kb.adjust(1)

    # Пагинация
    nav_btns: list[dict] = []
    if page > 0:
        nav_btns.append(
            {
                "text": "◀ Пред.",
                "callback_data": f"adm:error_reports:{page - 1}:{status}",
            }
        )
    if (page + 1) * _PAGE_SIZE < total:
        nav_btns.append(
            {
                "text": "След. ▶",
                "callback_data": f"adm:error_reports:{page + 1}:{status}",
            }
        )
    for btn in nav_btns:
        kb.button(text=btn["text"], callback_data=btn["callback_data"])
    if nav_btns:
        kb.adjust(len(nav_btns))

    kb.button(text="◀️ Главное меню админки", callback_data="adm:main")
    return kb.as_markup()


async def _adm_error_reports(
    callback: CallbackQuery, pool: asyncpg.Pool, page: int, status: str
) -> None:
    """Показать список отчётов об ошибках."""
    offset = page * _PAGE_SIZE
    try:
        reports = await db.get_error_reports(
            pool, status=status, limit=_PAGE_SIZE, offset=offset
        )
        # Общий счётчик для пагинации
        if status == "all":
            total = await pool.fetchval("SELECT COUNT(*) FROM error_reports") or 0
        else:
            total = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM error_reports WHERE status=$1", status
                )
                or 0
            )
    except Exception as e:
        log_exc_swallow(log, "Ошибка загрузки error_reports")
        await callback.message.edit_text(
            f"❌ Не удалось загрузить отчёты: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
        return

    status_label = _ERR_STATUS_LABELS.get(status, status)
    lines = [
        f"🐛 <b>Отчёты об ошибках — {status_label}</b>",
        f"Всего: <b>{total}</b> · Страница {page + 1}",
        "",
    ]
    if not reports:
        lines.append("Нет отчётов с таким статусом.")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_error_reports_kb(reports, page, status, total),
    )


async def _adm_show_error_report(
    callback: CallbackQuery, pool: asyncpg.Pool, report_id: int
) -> None:
    """Показать детальный вид одного отчёта об ошибке."""
    _back_kb_err = InlineKeyboardBuilder()
    _back_kb_err.button(
        text="◀️ К списку отчётов", callback_data="adm:error_reports:0:new"
    )

    try:
        report = await db.get_error_report(pool, report_id)
    except Exception as e:
        log_exc_swallow(log, f"Ошибка загрузки отчёта #{report_id}")
        await callback.message.edit_text(
            f"❌ Ошибка загрузки отчёта #{report_id}: {_html.escape(str(e))}",
            parse_mode="HTML",
            reply_markup=_back_kb_err.as_markup(),
        )
        return

    if not report:
        await callback.message.edit_text(
            "❌ Отчёт не найден.",
            parse_mode="HTML",
            reply_markup=_back_kb_err.as_markup(),
        )
        return

    dt = (
        report["created_at"].strftime("%d.%m.%Y %H:%M")
        if report.get("created_at")
        else "?"
    )
    user_label = (
        f"@{report['username']}" if report.get("username") else f"id{report['user_id']}"
    )
    status_label = _ERR_STATUS_LABELS.get(report["status"], report["status"])
    notes_val = _html.escape(report["notes"]) if report.get("notes") else ""
    notes_block = f"\n📝 <b>Заметки:</b> {notes_val}" if notes_val else ""
    description_escaped = _html.escape(report["description"] or "")

    text = (
        f"🐛 <b>Отчёт #{report['id']}</b>\n\n"
        f"👤 Пользователь: {user_label} (<code>{report['user_id']}</code>)\n"
        f"📅 Дата: {dt}\n"
        f"🔖 Статус: {status_label}\n"
        f"{notes_block}\n\n"
        f"📋 <b>Описание:</b>\n{description_escaped}"
    )

    kb = InlineKeyboardBuilder()
    # Кнопки смены статуса
    for st, label in _ERR_STATUS_LABELS.items():
        if st != report["status"]:
            kb.button(
                text=f"→ {label}", callback_data=f"adm:err_status:{report_id}:{st}"
            )
    kb.adjust(2)
    kb.button(text="◀️ К списку отчётов", callback_data="adm:error_reports:0:new")
    kb.button(text="◀️ Главное меню", callback_data="adm:main")
    kb.adjust(1)

    # Если есть скриншот — отправляем фото отдельно, затем редактируем сообщение
    if report.get("screenshot_id"):
        try:
            await callback.message.answer_photo(
                photo=report["screenshot_id"],
                caption=f"📸 Скриншот к отчёту #{report['id']}",
            )
        except Exception:
            log_exc_swallow(
                log, f"Не удалось отправить скриншот для отчёта #{report_id}"
            )
        text += "\n\n📸 Скриншот отправлен выше."

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


async def _adm_set_error_report_status(
    callback: CallbackQuery, pool: asyncpg.Pool, report_id: int, new_status: str
) -> None:
    """Изменить статус отчёта об ошибке."""
    _back_kb_err = InlineKeyboardBuilder()
    _back_kb_err.button(
        text="◀️ К списку отчётов", callback_data="adm:error_reports:0:new"
    )

    try:
        ok = await db.update_error_report_status(pool, report_id, new_status)
    except Exception as e:
        log_exc_swallow(log, f"Ошибка обновления статуса отчёта #{report_id}")
        await callback.message.edit_text(
            f"❌ Ошибка при обновлении статуса: {_html.escape(str(e))}",
            parse_mode="HTML",
            reply_markup=_back_kb_err.as_markup(),
        )
        return

    if not ok:
        await callback.message.edit_text(
            "❌ Отчёт не найден.",
            parse_mode="HTML",
            reply_markup=_back_kb_err.as_markup(),
        )
        return

    # Перезагрузить детальный вид — новый статус сразу виден
    await _adm_show_error_report(callback, pool, report_id)


# ── New user tracker (called from start.py or inline) ─────────────────────────


async def notify_new_platform_user(
    bot, pool: asyncpg.Pool, user_id: int, username: str | None, first_name: str
) -> None:
    """Call this when a new user starts the management bot for the first time."""
    raw = os.getenv("ADMIN_IDS", "")
    admin_ids = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}
    if not _NOTIFY_NEW_USERS or not admin_ids:
        return
    try:
        total = await pool.fetchval("SELECT COUNT(*) FROM platform_users") or 0
    except Exception:
        total = (
            await pool.fetchval("SELECT COUNT(DISTINCT added_by) FROM managed_bots")
            or 0
        )
    label = f"@{username}" if username else first_name
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 <b>Новый пользователь!</b>\n\n"
                f"ID: <code>{user_id}</code>\n"
                f"Имя: {label}\n"
                f"Всего пользователей: <b>{total}</b>",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось отправить уведомление о новом пользователе админу",
                user_id=admin_id,
            )


# ── Activity Logs Admin Screen ────────────────────────────────────────────────

_LOG_PAGE_SIZE = 25

_EVENT_ICONS = {
    "command": "⌨️",
    "callback": "🖱",
    "message": "💬",
    "error": "❌",
}


def _logs_kb(source: str, sf: str | None, page: int, has_next: bool) -> InlineKeyboardBuilder:
    sf_str = sf or "none"
    kb = InlineKeyboardBuilder()
    # Filter tabs
    if source == "ui":
        kb.button(text="🖱 UI (сейчас)", callback_data="adm:logs")
        kb.button(text="⚙️ TG-операции", callback_data="adm:logs_ops")
    else:
        kb.button(text="🖱 UI", callback_data="adm:logs")
        kb.button(text="⚙️ TG-операции (сейчас)", callback_data="adm:logs_ops")
    # Error filter
    err_cb = f"adm:logs_err" if source == "ui" else "adm:logs_ops_err"
    if sf == "error":
        kb.button(text="🔴 Только ошибки (сейчас)", callback_data=f"adm:logs_p:{source}:none:0")
    else:
        kb.button(text="🔴 Только ошибки", callback_data=err_cb)
    kb.button(text="🔍 По пользователю", callback_data="adm:logs_find_user")
    # Pagination
    if page > 0:
        kb.button(text="◀️ Назад", callback_data=f"adm:logs_p:{source}:{sf_str}:{page - 1}")
    if has_next:
        kb.button(text="▶️ Далее", callback_data=f"adm:logs_p:{source}:{sf_str}:{page + 1}")
    kb.button(text="🔄 Обновить", callback_data=f"adm:logs_p:{source}:{sf_str}:{page}")
    kb.button(text="◀️ Операции", callback_data="adm:section_ops")
    nav_cols = (1 if (page == 0 and not has_next) else (2 if (page > 0 and has_next) else 1))
    kb.adjust(2, 1, 1, nav_cols, 1, 1)
    return kb


async def _adm_logs(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    source: str = "ui",
    status_filter: str | None = None,
    page: int = 0,
    owner_filter: int | None = None,
) -> None:
    offset = page * _LOG_PAGE_SIZE
    lines = []
    has_next = False

    if source == "ui":
        try:
            rows = await db.get_activity_feed(
                pool,
                owner_id=owner_filter,
                status_filter=status_filter,
                limit=_LOG_PAGE_SIZE + 1,
                offset=offset,
            )
        except Exception:
            rows = []
        has_next = len(rows) > _LOG_PAGE_SIZE
        rows = rows[:_LOG_PAGE_SIZE]

        title_parts = ["📊 <b>Логи действий (UI)</b>"]
        if owner_filter:
            title_parts.append(f" · uid:<code>{owner_filter}</code>")
        if status_filter == "error":
            title_parts.append(" · 🔴 ошибки")
        title_parts.append(f" · стр.{page + 1}")
        lines.append("".join(title_parts))
        lines.append("")

        if not rows:
            lines.append("Нет записей.")
        else:
            for r in rows:
                dt = r["occurred_at"].strftime("%d.%m %H:%M") if r.get("occurred_at") else "?"
                uid = r.get("owner_id") or "?"
                etype = r.get("event_type") or "?"
                icon = _EVENT_ICONS.get(etype, "•")
                action = _html.escape((r.get("action") or "")[:45])
                detail = r.get("detail") or ""
                detail_str = f" <i>{_html.escape(detail[:30])}</i>" if detail else ""
                status = r.get("status") or "ok"
                dur = r.get("duration_ms")
                dur_str = f" {dur}ms" if dur is not None else ""
                if status == "error":
                    err = r.get("error_msg") or ""
                    lines.append(
                        f"<code>{dt}</code> {icon} uid:{uid} <b>{action}</b>{detail_str} ❌{dur_str}"
                    )
                    if err:
                        lines.append(f"  └ <code>{_html.escape(err[:80])}</code>")
                else:
                    lines.append(
                        f"<code>{dt}</code> {icon} uid:{uid} {action}{detail_str} ✅{dur_str}"
                    )

        # Activity stats header
        try:
            stats = await db.get_activity_stats(pool)
            lines.insert(1,
                f"⚡ За час: {stats['last_hour']} событий · "
                f"👥 {stats['active_users_hour']} активных · "
                f"🔴 Ошибок/24ч: {stats['errors_day']}"
            )
        except Exception:
            pass

    else:  # ops
        try:
            rows = await db.get_account_ops_feed(
                pool,
                owner_id=owner_filter,
                status_filter=status_filter,
                limit=_LOG_PAGE_SIZE + 1,
                offset=offset,
            )
        except Exception:
            rows = []
        has_next = len(rows) > _LOG_PAGE_SIZE
        rows = rows[:_LOG_PAGE_SIZE]

        title_parts = ["⚙️ <b>Логи TG-операций</b>"]
        if owner_filter:
            title_parts.append(f" · uid:<code>{owner_filter}</code>")
        if status_filter == "error":
            title_parts.append(" · 🔴 ошибки")
        title_parts.append(f" · стр.{page + 1}")
        lines.append("".join(title_parts))
        lines.append("")

        if not rows:
            lines.append("Нет записей.")
        else:
            for r in rows:
                dt = r["occurred_at"].strftime("%d.%m %H:%M") if r.get("occurred_at") else "?"
                uid = r.get("owner_id") or "?"
                action = _html.escape((r.get("action") or "")[:30])
                target = r.get("target") or ""
                target_str = f" → <code>{_html.escape(target[:25])}</code>" if target else ""
                result = r.get("result") or "?"
                dur = r.get("duration_ms")
                dur_str = f" {dur}ms" if dur is not None else ""
                flood = r.get("flood_wait_s")
                flood_str = f" ⏳{flood}s" if flood else ""
                if result == "success":
                    lines.append(f"<code>{dt}</code> ⚙️ uid:{uid} {action}{target_str} ✅{dur_str}")
                elif result == "flood_wait":
                    lines.append(f"<code>{dt}</code> ⚙️ uid:{uid} {action}{target_str} ⚠️{flood_str}")
                else:
                    err = r.get("error_msg") or ""
                    lines.append(f"<code>{dt}</code> ⚙️ uid:{uid} {action}{target_str} ❌{dur_str}")
                    if err:
                        lines.append(f"  └ <code>{_html.escape(err[:70])}</code>")

    text = "\n".join(lines)
    # Telegram message limit guard
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>...обрезано</i>"

    kb = _logs_kb(source, status_filter, page, has_next)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
