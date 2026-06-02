"""Super-admin panel — platform management, monitoring, token vault, user control."""
from __future__ import annotations
import asyncio
import csv
import io
import logging
import os
from datetime import datetime, timedelta

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

def _admin_main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пользователи платформы", callback_data="adm:users")
    kb.button(text="💳 Подписки & платежи",     callback_data="adm:subs")
    kb.button(text="🤖 Все боты & токены",       callback_data="adm:bots")
    kb.button(text="📊 Системная статистика",    callback_data="adm:stats")
    kb.button(text="📈 Операции платформы",      callback_data="adm:platform_ops")
    kb.button(text="💰 Цены на подписки",        callback_data="adm:prices")
    kb.button(text="⚙️ Методы оплаты",          callback_data="adm:pay_cfg")
    kb.button(text="📨 Рассылка всем юзерам",    callback_data="adm:broadcast")
    _notify_icon = "✅" if _NOTIFY_NEW_USERS else "❌"
    kb.button(text=f"🔔 Уведомления о новых {_notify_icon}", callback_data="adm:notify_toggle")
    _free_icon = "✅ ВКЛ" if get_free_mode() else "❌ ВЫКЛ"
    kb.button(text=f"🆓 Free Mode: {_free_icon}", callback_data="adm:free_mode_toggle")
    kb.button(text="🚫 Заблокировать юзера",     callback_data="adm:block_ask")
    kb.button(text="✅ Разблокировать юзера",    callback_data="adm:unblock_ask")
    kb.button(text="🗑 Удалить данные юзера",    callback_data="adm:delete_ask")
    kb.button(text="💰 Выдать подписку",         callback_data="adm:grant_ask")
    kb.button(text="❌ Забрать подписку",        callback_data="adm:revoke_ask")
    kb.button(text="💰 Bulk-выдача подписок",    callback_data="adm:bulk_grant_ask")
    kb.button(text="⚔️ Выдать Strike доступ",   callback_data="adm:strike_grant_ask")
    kb.button(text="⚔️ Забрать Strike доступ",  callback_data="adm:strike_revoke_ask")
    kb.button(text="📁 Экспорт токенов (файл)", callback_data="adm:tokens_file")
    kb.button(text="📋 Экспорт юзеров (CSV)",   callback_data="adm:users_csv")
    kb.button(text="🔍 Поиск юзера",            callback_data="adm:find_user")
    kb.button(text="⚙️ Системный режим Swarm",   callback_data="adm:swarm_mode")
    kb.button(text="🧹 Очистка данных",          callback_data="adm:cleanup_ask")
    kb.button(text="🔑 Переменные Railway",      callback_data="adm:env_list")
    kb.button(text="◀️ Выйти из админки",        callback_data="adm:exit")
    kb.adjust(2)
    return kb.as_markup()


# ── Список отображаемых переменных (с метками) ────────────────────────────────

_ENV_VARS: list[tuple[str, str]] = [
    ("MANAGER_BOT_TOKEN",      "🤖 Bot Token"),
    ("ADMIN_IDS",              "👑 Admin IDs"),
    ("ADMIN_SECRET",           "🔐 Admin Secret"),
    ("TON_WALLET",             "💎 TON Wallet"),
    ("TON_API_KEY",            "🔑 TON API Key"),
    ("TRON_WALLET",            "💵 TRON Wallet"),
    ("TRON_API_KEY",           "🔑 TRON API Key"),
    ("OPENROUTER_API_KEY",     "🤖 OpenRouter Key"),
    ("OPENROUTER_MODEL",       "🧠 OpenRouter Model"),
    ("ANTHROPIC_API_KEY",      "🧠 Anthropic Key"),
    ("TG_API_ID",              "📱 TG API ID"),
    ("TG_API_HASH",            "📱 TG API Hash"),
    ("BROADCAST_DELAY",        "⏱ Broadcast Delay"),
    ("RAILWAY_TOKEN",          "🚂 Railway Token"),
    ("RAILWAY_PROJECT_ID",     "🚂 Railway Project ID"),
]
_ENV_KEYS = {k for k, _ in _ENV_VARS}


def _env_list_kb(vars_online: dict[str, str] | None = None):
    kb = InlineKeyboardBuilder()
    for key, label in _ENV_VARS:
        val = (vars_online or {}).get(key, "")
        status = "✅" if val else "❌"
        kb.button(text=f"{status} {label}", callback_data=f"adm:env_edit:{key}")
    kb.button(text="➕ Добавить переменную",   callback_data="adm:env_add")
    kb.button(text="🔄 Обновить список",        callback_data="adm:env_list")
    kb.button(text="◀️ Главное меню админки",  callback_data="adm:main")
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
        log_exc_swallow(log, "Не удалось удалить сообщение с секретной фразой", user_id=message.from_user.id)
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
    total_users = await pool.fetchval("SELECT COUNT(DISTINCT added_by) FROM managed_bots") or 0
    total_bots  = await pool.fetchval("SELECT COUNT(*) FROM managed_bots") or 0
    total_subs  = await pool.fetchval(
        "SELECT COUNT(*) FROM subscriptions WHERE is_active=true AND expires_at > now()"
    ) or 0
    total_payments = await pool.fetchval(
        "SELECT COUNT(*) FROM payments WHERE status='confirmed'"
    ) or 0
    revenue = await pool.fetchval(
        "SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='confirmed'"
    ) or 0

    try:
        total_users = await pool.fetchval("SELECT COUNT(*) FROM platform_users") or 0
    except Exception:
        log_exc_swallow(log, "Не удалось получить количество пользователей из platform_users",
                        user_id=msg_or_cb.from_user.id)
        # total_users already set above as fallback
    try:
        today_users = await pool.fetchval(
            "SELECT COUNT(*) FROM platform_users "
            "WHERE COALESCE(registered_at, first_seen) >= CURRENT_DATE"
        ) or 0
    except Exception:
        today_users = 0

    text = (
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b> (+{today_users} сегодня)\n"
        f"🤖 Ботов в системе: <b>{total_bots}</b>\n"
        f"💳 Активных подписок: <b>{total_subs}</b>\n"
        f"✅ Оплат подтверждено: <b>{total_payments}</b>\n"
        f"💰 Выручка (USD): <b>${float(revenue):.2f}</b>\n\n"
        f"📅 {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC"
    )
    if edit and hasattr(msg_or_cb, "message"):
        try:
            await msg_or_cb.message.edit_text(text, parse_mode="HTML",
                                               reply_markup=_admin_main_kb())
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
    else:
        target = msg_or_cb if hasattr(msg_or_cb, "answer") else msg_or_cb.message
        await target.answer(text, parse_mode="HTML", reply_markup=_admin_main_kb())


# ── Callback dispatcher ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(callback: CallbackQuery, pool: asyncpg.Pool,
                    http: aiohttp.ClientSession) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа.", show_alert=True)
        return
    await callback.answer()
    action = callback.data.removeprefix("adm:")

    if action == "main":
        await _show_admin_main(callback, pool, edit=True)

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
            parse_mode="HTML", reply_markup=_back_kb(),
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
        await db.set_platform_setting(pool, "free_mode", "true" if new_state else "false")
        status = "включён ✅ — все функции бесплатны" if new_state else "выключен ⛔ — стандартные тарифы"
        await callback.answer(f"🆓 Free Mode {status}", show_alert=True)
        await _show_admin_main(callback, pool, edit=True)

    elif action == "block_ask":
        await callback.message.edit_text(
            "🚫 <b>Заблокировать пользователя</b>\n\nОтправьте Telegram ID (число):",
            parse_mode="HTML", reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'block','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='block',data=''",
            callback.from_user.id,
        )

    elif action == "unblock_ask":
        await callback.message.edit_text(
            "✅ <b>Разблокировать пользователя</b>\n\nОтправьте Telegram ID:",
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
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
        from bot.handlers.subscription import _payment_settings_text, _payment_settings_kb
        await callback.message.edit_text(
            _payment_settings_text(), parse_mode="HTML", reply_markup=_payment_settings_kb(),
        )

    elif action == "swarm_mode":
        await _adm_swarm_mode(callback, pool)

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
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'bulk_grant','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='bulk_grant',data=''",
            callback.from_user.id,
        )

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
            parse_mode="HTML", reply_markup=_back_kb(),
        )
        await pool.execute(
            "INSERT INTO admin_state(admin_id,state,data) VALUES($1,'cleanup','') "
            "ON CONFLICT(admin_id) DO UPDATE SET state='cleanup',data=''",
            callback.from_user.id,
        )

    elif action == "audit_log":
        try:
            rows = await pool.fetch(
                """SELECT occurred_at, user_id, operation, status, details
                   FROM operation_audit
                   ORDER BY occurred_at DESC LIMIT 20"""
            )
        except Exception:
            rows = []
        if rows:
            lines = []
            for r in rows:
                dt = r["occurred_at"].strftime("%d.%m %H:%M") if r.get("occurred_at") else "?"
                uid = r.get("user_id") or "?"
                op = r.get("operation") or "?"
                status = r.get("status") or "?"
                lines.append(f"<code>{dt}</code> uid:{uid} {op} [{status}]")
            text = "🔐 <b>Аудит логи (последние 20)</b>\n\n" + "\n".join(lines)
        else:
            text = "🔐 <b>Аудит логи</b>\n\nЗаписей нет или таблица пуста."
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="adm:users")
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

    elif action == "exit":
        await callback.message.edit_text(
            "👋 Вышли из админки.",
            reply_markup=main_menu(is_admin=True),
        )


# ── Sub-screens ───────────────────────────────────────────────────────────────

async def _adm_users(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    from bot.handlers.admin_users import AdminUserCb
    _PLAN_EMO = {"free": "🆓", "starter": "⭐", "pro": "🚀", "enterprise": "👑"}
    try:
        rows = await pool.fetch(
            """SELECT pu.user_id, pu.username, pu.first_name,
                      COALESCE(pu.current_plan, 'free') as current_plan,
                      COALESCE(pu.is_banned, false) as is_banned,
                      s.plan as sub_plan, s.expires_at as sub_exp
               FROM platform_users pu
               LEFT JOIN subscriptions s
                 ON s.user_id=pu.user_id AND s.is_active=true AND s.expires_at > now()
               ORDER BY COALESCE(pu.registered_at, pu.first_seen) DESC NULLS LAST
               LIMIT 15"""
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ <code>{e}</code>", parse_mode="HTML", reply_markup=_back_kb()
        )
        return
    total = await pool.fetchval("SELECT COUNT(*) FROM platform_users") or 0
    kb = InlineKeyboardBuilder()
    lines = []
    for r in rows:
        plan = r["sub_plan"] or r["current_plan"] or "free"
        emo = _PLAN_EMO.get(plan, "❓")
        ban = "🚫 " if r["is_banned"] else ""
        name = f"@{r['username']}" if r["username"] else r["first_name"] or f"#{r['user_id']}"
        exp = ""
        if r["sub_exp"]:
            exp = f" до {r['sub_exp'].strftime('%d.%m')}"
        lines.append(f"{ban}{emo} {name} — {plan.upper()}{exp}")
        kb.button(
            text=f"{ban}{emo} {name[:22]}",
            callback_data=AdminUserCb(action="user_actions", user_id=r["user_id"])
        )
    body = "\n".join(lines) if lines else "Нет зарегистрированных пользователей."
    kb.button(text="📋 Полный список", callback_data=AdminUserCb(action="list"))
    kb.button(text="📥 Экспорт CSV", callback_data=AdminUserCb(action="export_csv"))
    kb.button(text="◀️ Назад", callback_data="adm:main")
    kb.adjust(1)
    await callback.message.edit_text(
        f"👥 <b>Пользователи платформы</b> (всего: <b>{total}</b>)\n\n{body}",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


async def _adm_subscriptions(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    active = await pool.fetch(
        "SELECT user_id, plan, expires_at FROM subscriptions "
        "WHERE is_active=true AND expires_at > now() ORDER BY expires_at DESC LIMIT 20"
    )
    expired = await pool.fetchval(
        "SELECT COUNT(*) FROM subscriptions WHERE is_active=false OR expires_at <= now()"
    ) or 0
    lines = []
    for s in active:
        lines.append(
            f"<code>{s['user_id']}</code> — <b>{s['plan'].upper()}</b> "
            f"до {s['expires_at'].strftime('%d.%m.%Y')}"
        )
    body = "\n".join(lines) if lines else "Активных подписок нет."
    await callback.message.edit_text(
        f"💳 <b>Активные подписки</b>\n\n{body}\n\n"
        f"<i>Истёкших: {expired}</i>",
        parse_mode="HTML", reply_markup=_back_kb(),
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
            f"<code>{b['bot_id']}</code> {label} "
            f"(owner: <code>{b['added_by']}</code>)"
        )
    body = "\n".join(lines) if lines else "Ботов нет."
    await callback.message.edit_text(
        f"🤖 <b>Последние 20 ботов в системе</b>\n\n{body}\n\n"
        "Для полного списка с токенами нажмите «Экспорт токенов».",
        parse_mode="HTML", reply_markup=_back_kb(),
    )


async def _adm_system_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    total_msgs = await pool.fetchval("SELECT COALESCE(SUM(sent_count),0) FROM broadcasts") or 0
    total_bc   = await pool.fetchval("SELECT COUNT(*) FROM broadcasts") or 0
    total_relay = await pool.fetchval("SELECT COUNT(*) FROM relay_sessions") or 0
    total_funnels = await pool.fetchval("SELECT COUNT(*) FROM funnels") or 0
    total_schedules = await pool.fetchval(
        "SELECT COUNT(*) FROM scheduled_broadcasts WHERE status='pending'"
    ) or 0
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
        parse_mode="HTML", reply_markup=_back_kb(),
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
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
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
        writer.writerow([
            r["added_by"], r["bots"],
            r["plan"] or "free",
            r["expires_at"].strftime("%Y-%m-%d") if r["expires_at"] else "",
        ])
    content = buf.getvalue().encode("utf-8")
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
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
        kb.button(text=f"✏️ {emo} {plan.upper()} — ${price}/мес", callback_data=f"adm:price_edit:{plan}")
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
        parse_mode="HTML", reply_markup=kb.as_markup(),
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
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )
    await pool.execute(
        "INSERT INTO admin_state(admin_id,state,data) VALUES($1,$2,'') "
        "ON CONFLICT(admin_id) DO UPDATE SET state=$2,data=''",
        callback.from_user.id, f"price_edit:{plan}",
    )


async def _adm_swarm_mode(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    current = await db.get_system_mode(pool)
    modes = {
        "manual":     "🟢 Manual — ручной контроль",
        "assisted":   "🟡 Assisted — система предлагает",
        "autopilot":  "🔵 Autopilot — авто-оптимизация",
        "growth":     "🔴 Growth — агрессивный рост",
        "experiment": "🟣 Experiment — макс. тестирование",
        "stability":  "⚫ Stability — фиксированный роутинг",
    }
    kb = InlineKeyboardBuilder()
    for mode, label in modes.items():
        prefix = "✅ " if mode == current else ""
        kb.button(text=f"{prefix}{label}", callback_data=f"adm:set_mode:{mode}")
    kb.button(text="◀️ Назад", callback_data="adm:main")
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚙️ <b>Swarm системный режим</b>\n\nТекущий: <b>{current.upper()}</b>\n\n"
        "Режим определяет поведение всего swarm-роутинга:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Message handler for admin FSM states ─────────────────────────────────────

@router.message(F.text)
async def handle_admin_message(message: Message, pool: asyncpg.Pool,
                                http: aiohttp.ClientSession) -> None:
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

    await pool.execute("DELETE FROM admin_state WHERE admin_id=$1", message.from_user.id)

    if state == "broadcast":
        users = await pool.fetch(
            "SELECT DISTINCT added_by FROM managed_bots"
        )
        sent = 0
        for u in users:
            try:
                await message.bot.send_message(u["added_by"], text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                log_exc_swallow(log, "Не удалось отправить сообщение рассылки пользователю",
                                user_id=u["added_by"])
        await message.answer(
            f"✅ Рассылка завершена\n\nОтправлено: <b>{sent}</b> / {len(users)}",
            parse_mode="HTML", reply_markup=_admin_main_kb(),
        )

    elif state == "block":
        try:
            uid = int(text)
            await pool.execute(
                "INSERT INTO blocked_users(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid
            )
            await message.answer(
                f"🚫 Пользователь <code>{uid}</code> заблокирован.",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "unblock":
        try:
            uid = int(text)
            await pool.execute("DELETE FROM blocked_users WHERE user_id=$1", uid)
            await message.answer(
                f"✅ Пользователь <code>{uid}</code> разблокирован.",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "delete_user":
        try:
            uid = int(text)
            bot_ids = await pool.fetch("SELECT bot_id FROM managed_bots WHERE added_by=$1", uid)
            for b in bot_ids:
                await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", b["bot_id"])
            await pool.execute("DELETE FROM subscriptions WHERE user_id=$1", uid)
            await pool.execute("DELETE FROM payments WHERE user_id=$1", uid)
            await message.answer(
                f"🗑 Данные пользователя <code>{uid}</code> удалены "
                f"({len(bot_ids)} ботов).",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
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
            expires = datetime.utcnow() + timedelta(days=30 * months)
            await pool.execute(
                """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
                   VALUES($1,$2,$3,true)
                   ON CONFLICT(user_id) DO UPDATE
                   SET plan=$2, expires_at=$3, is_active=true""",
                uid, plan, expires,
            )
            await message.answer(
                f"✅ Подписка <b>{plan.upper()}</b> выдана пользователю "
                f"<code>{uid}</code> на {months} мес.",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
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
                log_exc_swallow(log, "Не удалось уведомить пользователя о выдаче подписки",
                                user_id=uid)
        except (ValueError, IndexError):
            await message.answer(
                "❌ Формат: <code>USER_ID план месяцев</code>\n"
                "Пример: <code>123456 pro 3</code>",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
            )

    elif state == "find":
        try:
            uid = int(text)
            bots = await pool.fetch(
                "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1", uid
            )
            sub = await pool.fetchrow(
                "SELECT plan, expires_at FROM subscriptions "
                "WHERE user_id=$1 AND is_active=true AND expires_at > now()", uid
            )
            plan_info = (
                f"{sub['plan'].upper()} до {sub['expires_at'].strftime('%d.%m.%Y')}"
                if sub else "FREE"
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
                parse_mode="HTML", reply_markup=_admin_main_kb(),
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
                expires = datetime.utcnow() + timedelta(days=30 * months)
                await pool.execute(
                    """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
                       VALUES($1,$2,$3,true)
                       ON CONFLICT(user_id) DO UPDATE
                       SET plan=$2, expires_at=$3, is_active=true""",
                    uid, plan, expires,
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
                    log_exc_swallow(log, "Не удалось уведомить пользователя о массовой выдаче подписки",
                                    user_id=uid)
            except (ValueError, IndexError) as e:
                fail_list.append(f"❌ {line[:30]}: {e}")
        result_lines = ok_list[:20] + fail_list[:10]
        await message.answer(
            f"💰 <b>Массовая выдача завершена</b>\n\n"
            f"Успешно: <b>{len(ok_list)}</b>, ошибок: <b>{len(fail_list)}</b>\n\n"
            + "\n".join(result_lines),
            parse_mode="HTML", reply_markup=_admin_main_kb(),
        )

    elif state == "revoke":
        try:
            uid = int(text.strip())
            await db.revoke_plan_from_user(pool, uid, message.from_user.id)
            await message.answer(
                f"❌ Подписка отозвана у пользователя <code>{uid}</code>.\n"
                f"Пользователь переведён на план <b>FREE</b>.",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
            )
            try:
                await message.bot.send_message(
                    uid,
                    "ℹ️ <b>Ваша подписка была отозвана администратором.</b>\n\n"
                    "Вы переведены на план FREE.\n"
                    "Для восстановления доступа оформите подписку в /menu → 💳 Billing.",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Не удалось уведомить пользователя об отзыве подписки",
                                user_id=uid)
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
                target_uid, message.from_user.id,
            )
            await message.answer(
                f"⚔️ <b>Strike доступ активирован</b>\n\n"
                f"Пользователь <code>{target_uid}</code> теперь имеет доступ к Strike Module.",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
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
                log_exc_swallow(log, "Не удалось уведомить пользователя о выдаче Strike доступа",
                                user_id=target_uid)
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "strike_revoke":
        try:
            target_uid = int(text.strip())
            await db.revoke_strike_access(pool, target_uid, message.from_user.id)
            await message.answer(
                f"⚔️ <b>Strike доступ отозван</b>\n\n"
                f"У пользователя <code>{target_uid}</code> больше нет доступа к Strike Module.",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
            )
            try:
                await message.bot.send_message(
                    target_uid,
                    "ℹ️ <b>Strike доступ был отозван администратором.</b>\n\n"
                    "Для получения доступа обратитесь к администратору.",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "Не удалось уведомить пользователя об отзыве Strike доступа",
                                user_id=target_uid)
        except ValueError:
            await message.answer("❌ Неверный ID.", reply_markup=_admin_main_kb())

    elif state == "cleanup":
        if text.strip().upper() != "CLEAN":
            await message.answer("❌ Отменено (введите CLEAN для подтверждения).", reply_markup=_admin_main_kb())
            return
        try:
            flood_del = await pool.fetchval(
                "WITH d AS (DELETE FROM account_flood_log WHERE created_at < now() - INTERVAL '30 days' RETURNING 1) SELECT COUNT(*) FROM d"
            ) or 0
        except Exception:
            flood_del = 0
        try:
            ops_del = await pool.fetchval(
                "WITH d AS (DELETE FROM operation_queue WHERE status IN ('done','failed') "
                "AND finished_at < now() - INTERVAL '7 days' RETURNING 1) SELECT COUNT(*) FROM d"
            ) or 0
        except Exception:
            ops_del = 0
        try:
            audit_del = await pool.fetchval(
                "WITH d AS (DELETE FROM operation_audit WHERE occurred_at < now() - INTERVAL '30 days' RETURNING 1) SELECT COUNT(*) FROM d"
            ) or 0
        except Exception:
            audit_del = 0
        try:
            dm_del = await pool.fetchval(
                "WITH d AS (DELETE FROM dm_campaign_log WHERE sent_at < now() - INTERVAL '90 days' RETURNING 1) SELECT COUNT(*) FROM d"
            ) or 0
        except Exception:
            dm_del = 0
        await message.answer(
            f"🧹 <b>Очистка завершена</b>\n\n"
            f"• Флуд-логов удалено: <b>{flood_del}</b>\n"
            f"• Операций удалено: <b>{ops_del}</b>\n"
            f"• Аудит-записей удалено: <b>{audit_del}</b>\n"
            f"• DM-логов удалено: <b>{dm_del}</b>",
            parse_mode="HTML", reply_markup=_admin_main_kb(),
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
                    await railway_api.set_variable(tmp, f"PRICE_{plan.upper()}", str(price))
                note = "Сохранено в Railway."
            except Exception:
                note = "⚠️ Railway не настроен — цена активна до перезапуска."
            await message.answer(
                f"✅ Цена <b>{plan.upper()}</b> обновлена: <b>${price}/мес</b>\n\n{note}",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
            )
        except ValueError:
            await message.answer("❌ Введите целое число от 1 до 9999", reply_markup=_admin_main_kb())

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
                    parse_mode="HTML", reply_markup=_admin_main_kb(),
                )
            except Exception as e:
                await message.answer(
                    f"❌ <b>Ошибка Railway API</b>\n\n<code>{e}</code>",
                    parse_mode="HTML", reply_markup=_admin_main_kb(),
                )

    elif state == "env_add":
        parts = text.split(None, 1)
        if len(parts) != 2:
            await message.answer(
                "❌ Неверный формат. Нужно: <code>КЛЮЧ значение</code>",
                parse_mode="HTML", reply_markup=_admin_main_kb(),
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
                    parse_mode="HTML", reply_markup=_admin_main_kb(),
                )
            except Exception as e:
                await message.answer(
                    f"❌ <b>Ошибка Railway API</b>\n\n<code>{e}</code>",
                    parse_mode="HTML", reply_markup=_admin_main_kb(),
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
            parse_mode="HTML", reply_markup=_back_kb(),
        )
        return

    try:
        vars_online = await railway_api.list_variables(http)
    except Exception as e:
        await callback.message.edit_text(
            f"❌ <b>Ошибка Railway API</b>\n\n<code>{e}</code>",
            parse_mode="HTML", reply_markup=_back_kb(),
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
    kb.button(text="◀️ Назад",              callback_data="adm:env_list")
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
        extra_hint = "\n💡 Получить: railway.com → Account Settings → Tokens → Create Token"
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
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )
    await pool.execute(
        "INSERT INTO admin_state(admin_id,state,data) VALUES($1,$2,'') "
        "ON CONFLICT(admin_id) DO UPDATE SET state=$2,data=''",
        callback.from_user.id, f"env_edit:{key}",
    )


async def _adm_env_delete(
    callback: CallbackQuery, http: aiohttp.ClientSession, key: str
) -> None:
    try:
        await railway_api.delete_variable(http, key)
        os.environ.pop(key, None)
        await callback.answer(f"✅ {key} удалена", show_alert=True)
        await _adm_env_list(callback, http)
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)


# ── Platform operations analytics ────────────────────────────────────────────

async def _adm_platform_ops(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Платформенная аналитика по операциям всех пользователей."""
    try:
        total_ops = await pool.fetchval("SELECT COUNT(*) FROM operation_queue") or 0
        running = await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE status='running'"
        ) or 0
        pending = await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE status='pending'"
        ) or 0
        done_today = await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE status='done' "
            "AND finished_at > now() - INTERVAL '24 hours'"
        ) or 0
        failed_today = await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE status='failed' "
            "AND finished_at > now() - INTERVAL '24 hours'"
        ) or 0
        top_ops = await pool.fetch(
            """SELECT op_type, COUNT(*) AS cnt
               FROM operation_queue
               WHERE created_at > now() - INTERVAL '7 days'
               GROUP BY op_type ORDER BY cnt DESC LIMIT 5"""
        )
        total_floods = await pool.fetchval(
            "SELECT COUNT(*) FROM account_flood_log WHERE created_at > now() - INTERVAL '24 hours'"
        ) or 0
        active_accounts = await pool.fetchval(
            "SELECT COUNT(DISTINCT owner_id) FROM tg_accounts WHERE is_active=true"
        ) or 0
        dm_sent = await pool.fetchval(
            "SELECT COUNT(*) FROM dm_campaign_log WHERE status='sent' "
            "AND sent_at > now() - INTERVAL '24 hours'"
        ) or 0
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка получения данных: {e}", parse_mode="HTML", reply_markup=_back_kb()
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


# ── New user tracker (called from start.py or inline) ─────────────────────────

async def notify_new_platform_user(bot, pool: asyncpg.Pool, user_id: int,
                                    username: str | None, first_name: str) -> None:
    """Call this when a new user starts the management bot for the first time."""
    raw = os.getenv("ADMIN_IDS", "")
    admin_ids = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}
    if not _NOTIFY_NEW_USERS or not admin_ids:
        return
    try:
        total = await pool.fetchval("SELECT COUNT(*) FROM platform_users") or 0
    except Exception:
        total = await pool.fetchval("SELECT COUNT(DISTINCT added_by) FROM managed_bots") or 0
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
            log_exc_swallow(log, "Не удалось отправить уведомление о новом пользователе админу",
                            user_id=admin_id)
