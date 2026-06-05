import asyncio
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
import asyncpg
from bot.keyboards import main_menu
from bot.utils.subscription import get_plan, PLAN_EMOJIS, is_platform_admin
from database import db
from bot.callbacks import BotCb
from bot.handlers.admin import notify_new_platform_user
from services.logger import log_exc_swallow
import logging

log = logging.getLogger(__name__)

router = Router()


async def _record_reentry_safe(pool, uid: int, days_absent: float) -> None:
    try:
        from services import behavioral_engine

        await behavioral_engine.record_reentry(pool, uid, "platform", uid, days_absent)
    except Exception as e:
        log.debug("record_reentry failed: %s", e)


BUILD_VERSION = "2026.06.03-r35"


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    await message.answer(
        f"🔖 <b>BotMother OS</b> build <code>{BUILD_VERSION}</code>", parse_mode="HTML"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Нет активного действия для отмены.",
            reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)),
        )
        return
    await state.clear()
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    admin = is_platform_admin(uid)

    # Parallel: blocked check + user info + bots list (all independent reads)
    try:
        blocked_val, existing, bots = await asyncio.gather(
            pool.fetchval("SELECT 1 FROM blocked_users WHERE user_id=$1", uid),
            db.get_user_info(pool, uid),
            db.get_bots(pool, uid),
            return_exceptions=True,
        )
    except Exception:
        blocked_val, existing, bots = None, None, []

    if isinstance(blocked_val, BaseException):
        log_exc_swallow(log, "Не удалось проверить блокировку пользователя")
        blocked_val = None
    if isinstance(existing, BaseException):
        existing = None
    if isinstance(bots, BaseException):
        bots = []

    if blocked_val:
        await message.answer("⛔️ Ваш аккаунт заблокирован. Обратитесь в поддержку.")
        return

    is_new = False
    try:
        is_new = existing is None
        await db.register_or_update_user(
            pool,
            uid,
            message.from_user.username,
            message.from_user.first_name or "",
        )
        if is_new:
            await notify_new_platform_user(
                message.bot,
                pool,
                uid,
                message.from_user.username,
                message.from_user.first_name or "",
            )
        elif existing and (existing.get("last_seen") or existing.get("last_active")):
            # Record reentry if user was absent 7+ days
            last = existing.get("last_seen") or existing.get("last_active")
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days_absent = (datetime.now(timezone.utc) - last).total_seconds() / 86400
            if days_absent >= 7:
                asyncio.create_task(_record_reentry_safe(pool, uid, days_absent))
    except Exception:
        log_exc_swallow(log, "Не удалось зарегистрировать или обновить пользователя")

    # Handle referral code from /start inv_XXXXXX
    if is_new:
        args = message.text.split(maxsplit=1)
        start_param = args[1].strip() if len(args) > 1 else ""
        if start_param.startswith("inv_"):
            try:
                referrer_id = await db.get_user_by_referral_code(pool, start_param)
                if referrer_id and referrer_id != uid:
                    recorded = await db.record_platform_referral(pool, referrer_id, uid)
                    if recorded:
                        await db.give_welcome_bonus(pool, uid, message.bot)
            except Exception as e:
                log.warning("Referral processing error: %s", e)

    # bots already fetched in parallel above
    bot_count = len(bots)

    if not bot_count:
        await message.answer(
            "👋 <b>Добро пожаловать в BotMother!</b>\n\n"
            "Это система управления Telegram-активами:\n"
            "боты, каналы, группы, аккаунты — всё в одном месте.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🚀 <b>Быстрый старт — выберите сценарий:</b>\n\n"
            "🤖 <b>Хочу управлять ботом</b>\n"
            "→ ➕ Добавить бота → вставить токен от @BotFather\n"
            "→ Рассылки, аудитория, авто-ответы, CRM\n\n"
            "📡 <b>Хочу управлять каналами</b>\n"
            "→ /menu → 📱 Активы → 📡 Каналы\n"
            "→ Импорт, создание, публикация во все каналы\n\n"
            "📱 <b>Хочу операции через аккаунт</b>\n"
            "→ /accounts → добавить аккаунт\n"
            "→ Создание каналов/групп, вступление, публикация\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>Полное меню OS:</b> /menu\n"
            "❓ <b>Помощь:</b> /help\n\n"
            f"<i>ID: <code>{uid}</code></i>",
            parse_mode="HTML",
            reply_markup=main_menu(is_admin=admin),
        )
        return

    total_aud = sum(b["audience_count"] for b in bots if "audience_count" in b.keys())

    active_broadcasts = 0
    try:
        bot_ids = [b["bot_id"] for b in bots]
        active_broadcasts = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM broadcasts WHERE bot_id = ANY($1::bigint[]) AND status IN ('pending', 'running')",
                bot_ids,
            )
            or 0
        )
    except Exception:
        log_exc_swallow(log, "Не удалось получить количество активных рассылок")
        active_broadcasts = 0

    stats_lines = [
        f"🤖 Ботов: <b>{bot_count}</b>",
        f"👥 Аудитория: <b>{total_aud}</b> чел.",
    ]
    if active_broadcasts:
        stats_lines.append(f"📢 Активных рассылок: <b>{active_broadcasts}</b>")
    summary = " · ".join(stats_lines[:2])
    extra = f"\n{stats_lines[2]}" if active_broadcasts else ""

    await message.answer(
        f"👋 <b>BotMother OS</b>  <code>v{BUILD_VERSION}</code>\n\n"
        f"{summary}{extra}\n\n"
        f"ID: <code>{uid}</code>\n\n"
        f"💡 Нажмите на бота из списка → откроется меню управления",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin=admin),
    )


@router.callback_query(BotCb.filter(F.action == "help"))
async def cb_help(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id
    try:
        plan = await get_plan(pool, uid)
    except Exception:
        log_exc_swallow(log, "Не удалось получить план пользователя для /help callback")
        plan = "free"
    emoji = PLAN_EMOJIS.get(plan, "🆓")

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))

    text = (
        f"❓ <b>Справка BotMother OS</b>\n\n"
        f"Ваш план: <b>{emoji} {plan.upper()}</b>\n\n"
        f"<b>📋 Команды:</b>\n"
        f"/start — главное меню\n"
        f"/subscription — подписка и оплата\n"
        f"/ranking — трекер позиций в поиске\n"
        f"/accounts — мои Telegram-аккаунты\n"
        f"/ops — операции с аккаунтами\n"
        f"/cancel — отменить текущее действие\n\n"
        f"<b>🤖 Разделы бота (открываются из меню бота):</b>\n"
        f"• Аудитория — список пользователей\n"
        f"• Рассылка — сообщение всем\n"
        f"• Команды, Шаблоны, Авто-ответы\n"
        f"• Inbox — живой чат (STARTER+)\n"
        f"• Цепочки — воронки (STARTER+)\n"
        f"• CRM, SEO, Диплинки (STARTER+)\n"
        f"• A/B тесты, Активность (PRO+)\n"
        f"• 📊 Позиции в поиске (STARTER+)\n\n"
        f"<b>🌐 Сеть &amp; операции</b> — управление всеми ботами сразу\n"
        f"<b>📡 Операции с аккаунтами</b> — через личный Telegram-аккаунт"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.message(Command("help"))
async def cmd_help(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    admin = is_platform_admin(uid)
    try:
        plan = await get_plan(pool, uid)
    except Exception:
        log_exc_swallow(log, "Не удалось получить план пользователя для /help команды")
        plan = "free"
    emoji = PLAN_EMOJIS.get(plan, "🆓")

    text = (
        f"❓ <b>Справка BotMother OS</b>\n\n"
        f"Ваш план: <b>{emoji} {plan.upper()}</b>\n\n"
        f"<b>📋 Команды:</b>\n"
        f"/start — главное меню\n"
        f"/subscription — подписка и оплата\n"
        f"/ranking — трекер позиций в поиске\n"
        f"/accounts — мои Telegram-аккаунты\n"
        f"/ops — операции с аккаунтами\n"
        f"/cancel — отменить текущее действие\n\n"
        f"<b>🤖 Разделы бота:</b>\n"
        f"Добавьте бота → выберите из списка → откроется меню:\n"
        f"• Аудитория, Рассылка, Команды, Шаблоны, Авто-ответы\n"
        f"• Inbox, Цепочки, CRM, SEO, Диплинки (STARTER+)\n"
        f"• A/B тесты, Активность, Мультигео (PRO+)\n"
        f"• 📊 Позиции в поиске Telegram (STARTER+)\n\n"
        f"<b>🌐 Сеть &amp; операции</b> — управление всеми ботами сразу\n"
        f"<b>📡 Операции с аккаунтами</b> — через личный Telegram-аккаунт\n\n"
        f"💡 Все функции с замком 🔒 открываются через /subscription"
    )
    await message.answer(
        text, parse_mode="HTML", reply_markup=main_menu(is_admin=admin)
    )
