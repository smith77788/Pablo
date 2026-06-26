"""Referral program dashboard for platform users."""

from __future__ import annotations

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb, RefCb, BmCb
from bot.utils.op_helpers import _progress_bar
from database import db

router = Router()

# Reward tier metadata (mirrors db._REWARD_TIERS)
_TIERS = [
    ("basic", "active", 5, "starter", 14, "🥉 Базовый"),
    ("silver", "paid", 3, "starter", 30, "🥈 Серебро"),
    ("gold", "paid", 10, "pro", 30, "🥇 Золото"),
    ("platinum", "paid", 25, "enterprise", 30, "💎 Платина"),
]
_PLAN_LABEL = {"starter": "Starter", "pro": "Pro", "enterprise": "Enterprise"}


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
            bar = _progress_bar(count, threshold, width=8)
            status = f"{bar} {count}/{threshold}"
        lines.append(f"{label} [{status}] — {days} дн. {_PLAN_LABEL[plan]} бесплатно")

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


def _dashboard_keyboard(share_url: str | None = None) -> object:
    kb = InlineKeyboardBuilder()
    if share_url:
        kb.button(
            text="📤 Поделиться ссылкой",
            url=f"https://t.me/share/url?url={share_url}&text=Присоединяйся+к+Infragram+по+моей+реферальной+ссылке!",
        )
    kb.button(text="🏆 Топ рефереров", callback_data=RefCb(action="leaderboard"))
    kb.button(text="◀️ Настройки", callback_data=BmCb(action="settings"))
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("referral"))
async def cmd_referral(message: Message) -> None:
    from bot.callbacks import BmCb

    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Открыть Infragram OS", callback_data=BmCb(action="main"))
    await message.answer(
        "👥 <b>Реферальная программа</b>\n\n"
        "Откройте BotMother OS и перейдите в:\n"
        "<code>/menu → ⚙️ Настройки → 👥 Рефералы</code>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(RefCb.filter(F.action == "menu"))
async def cb_ref_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id
    stats = await db.get_referral_stats(pool, uid)
    me = await callback.bot.get_me()
    bot_username = me.username or "bot"
    share_url = f"https://t.me/{bot_username}?start={stats['code']}"
    text = _build_dashboard(stats, bot_username)
    if stats["total"] == 0:
        text += (
            "\n\n💡 <b>Как начать?</b>\n"
            "Скопируйте свою реферальную ссылку и поделитесь ею с друзьями, "
            "коллегами или аудиторией. Каждый приглашённый получит 7 дней Starter бесплатно!"
        )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_dashboard_keyboard(share_url=share_url)
    )


@router.callback_query(RefCb.filter(F.action == "leaderboard"))
async def cb_ref_leaderboard(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    rows = await db.get_referral_leaderboard_platform(pool, limit=10)

    if not rows:
        text = (
            "🏆 <b>Топ рефереров</b>\n\nПока никто не пригласил платящих пользователей."
        )
    else:
        medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 7
        lines = ["🏆 <b>Топ рефереров (по платящим)</b>\n"]
        for i, row in enumerate(rows):
            name = (
                row.get("first_name")
                or row.get("username")
                or f"id:{row['referrer_id']}"
            )
            lines.append(
                f"{medals[i]} {name} — "
                f"💳 {row['paid_count']} платящих / "
                f"👥 {row['total_count']} всего"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=RefCb(action="menu"))
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )
