"""Referral program dashboard for platform users."""
from __future__ import annotations

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb, RefCb
from database import db

router = Router()

# Reward tier metadata (mirrors db._REWARD_TIERS)
_TIERS = [
    ("basic",    "active", 5,  "starter",    14, "🥉 Базовый"),
    ("silver",   "paid",   3,  "starter",    30, "🥈 Серебро"),
    ("gold",     "paid",   10, "pro",        30, "🥇 Золото"),
    ("platinum", "paid",   25, "enterprise", 30, "💎 Платина"),
]
_PLAN_LABEL = {"starter": "Starter", "pro": "Pro", "enterprise": "Enterprise"}


def _progress_bar(current: int, total: int, width: int = 8) -> str:
    filled = min(int(current / total * width), width)
    return "█" * filled + "░" * (width - filled)


def _build_dashboard(stats: dict, bot_username: str) -> str:
    code = stats["code"]
    link = f"https://t.me/{bot_username}?start={code}"
    total = stats["total"]
    active = stats["active"]
    paid = stats["paid"]
    granted = {r["level"] for r in stats["rewards"]}

    lines = [
        "🔗 <b>Реферальная программа</b>\n",
        f"Ваша ссылка:\n<code>{link}</code>\n",
        f"👥 Приглашено:  <b>{total}</b> чел.",
        f"✅ Активных:    <b>{active}</b> (создали бота)",
        f"💳 Платящих:   <b>{paid}</b>\n",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for level, metric, threshold, plan, days, label in _TIERS:
        count = active if metric == "active" else paid
        if level in granted:
            status = "✅ ПОЛУЧЕН"
            bar = "████████"
        else:
            bar = _progress_bar(count, threshold)
            status = f"{bar} {count}/{threshold}"
        lines.append(
            f"{label} [{status}]"
            f" — {days} дн. {_PLAN_LABEL[plan]} бесплатно"
        )

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "",
        "💡 <b>Каждый приглашённый получает 7 дней Starter бесплатно</b> — "
        "это мотивирует их делиться вашей ссылкой!",
        "",
        "📌 <b>Условия:</b>",
        "• <b>Активный</b> = пришёл по ссылке + создал бота за 7 дней",
        "• <b>Платящий</b> = совершил подтверждённый платёж",
        "• Каждый уровень выдаётся один раз навсегда",
    ]
    return "\n".join(lines)


def _dashboard_keyboard() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏆 Топ рефереров", callback_data=RefCb(action="leaderboard"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("referral"))
async def cmd_referral(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    stats = await db.get_referral_stats(pool, uid)
    me = await message.bot.get_me()
    text = _build_dashboard(stats, me.username or "bot")
    await message.answer(text, parse_mode="HTML", reply_markup=_dashboard_keyboard())


@router.callback_query(RefCb.filter(F.action == "menu"))
async def cb_ref_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id
    stats = await db.get_referral_stats(pool, uid)
    me = await callback.bot.get_me()
    text = _build_dashboard(stats, me.username or "bot")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_dashboard_keyboard())


@router.callback_query(RefCb.filter(F.action == "leaderboard"))
async def cb_ref_leaderboard(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    rows = await db.get_referral_leaderboard_platform(pool, limit=10)

    if not rows:
        text = "🏆 <b>Топ рефереров</b>\n\nПока никто не пригласил платящих пользователей."
    else:
        medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 7
        lines = ["🏆 <b>Топ рефереров (по платящим)</b>\n"]
        for i, row in enumerate(rows):
            name = row.get("first_name") or row.get("username") or f"id:{row['referrer_id']}"
            lines.append(
                f"{medals[i]} {name} — "
                f"💳 {row['paid_count']} платящих / "
                f"👥 {row['total_count']} всего"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=RefCb(action="menu"))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
