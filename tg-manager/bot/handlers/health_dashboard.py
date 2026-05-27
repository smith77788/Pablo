"""Health Dashboard — infrastructure health monitoring.

Entry point: HealthCb(action="menu")
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import HealthCb, BotCb

log = logging.getLogger(__name__)
router = Router()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=HealthCb(action="menu"))
    return kb


async def _fetch_account_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN is_active THEN 1 END) AS active,
            COUNT(CASE WHEN cooldown_until > now() THEN 1 END) AS in_cooldown,
            ROUND(AVG(COALESCE(trust_score, 1.0))::numeric, 2) AS avg_trust
        FROM tg_accounts
        WHERE owner_id=$1
        """,
        owner_id,
    )
    return dict(row) if row else {"total": 0, "active": 0, "in_cooldown": 0, "avg_trust": 0}


async def _fetch_flood_events_7d(pool: asyncpg.Pool, owner_id: int) -> int:
    try:
        val = await pool.fetchval(
            """
            SELECT COUNT(*) FROM account_flood_log afl
            JOIN tg_accounts ta ON ta.id = afl.account_id
            WHERE ta.owner_id=$1 AND afl.created_at > now() - interval '7 days'
            """,
            owner_id,
        )
        return int(val or 0)
    except Exception:
        return 0


# ── Menu ───────────────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "menu"))
async def cb_health_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    stats = await _fetch_account_stats(pool, user_id)
    flood_7d = await _fetch_flood_events_7d(pool, user_id)

    text = (
        "❤️ <b>Здоровье инфраструктуры</b>\n\n"
        f"📱 Всего аккаунтов: <b>{stats['total']}</b> (активных: <b>{stats['active']}</b>)\n"
        f"🌊 В кулдауне (flood): <b>{stats['in_cooldown']}</b>\n"
        f"⭐ Средний trust_score: <b>{stats['avg_trust']}</b>\n"
        f"📋 Flood events за 7 дней: <b>{flood_7d}</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Аккаунты",  callback_data=HealthCb(action="accounts"))
    kb.button(text="🤖 Боты",      callback_data=HealthCb(action="bots_health"))
    kb.button(text="🌊 Flood log", callback_data=HealthCb(action="flood_log"))
    kb.button(text="🔄 Обновить",  callback_data=HealthCb(action="menu"))
    kb.button(text="◀️ Назад",     callback_data=BotCb(action="main"))
    kb.adjust(2, 2, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Accounts health ────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "accounts"))
async def cb_health_accounts(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    rows = await pool.fetch(
        """
        SELECT id, phone, first_name, username, trust_score, cooldown_until,
               COALESCE(flood_count_7d, 0) AS flood_count_7d, is_active
        FROM tg_accounts
        WHERE owner_id=$1
        ORDER BY trust_score DESC NULLS LAST
        """,
        user_id,
    )

    lines = ["📱 <b>Здоровье аккаунтов</b>\n"]
    if not rows:
        lines.append("Нет подключённых аккаунтов.")
    else:
        now = datetime.now(timezone.utc)
        for acc in rows:
            trust = float(acc["trust_score"] or 1.0)
            phone = acc["phone"] or ""
            name = acc["username"] or acc["first_name"] or phone or f"id{acc['id']}"
            flood_until = acc["cooldown_until"]
            flood_cnt = int(acc["flood_count_7d"] or 0)

            if flood_until and flood_until.replace(tzinfo=timezone.utc) > now:
                time_str = flood_until.strftime("%H:%M")
                lines.append(f"❌ @{name} ({phone}) | <b>КУЛДАУН до {time_str}</b>")
            elif trust < 0.5:
                lines.append(
                    f"⚠️ @{name} ({phone}) | trust: <b>{trust:.2f}</b> | flood: {flood_cnt}"
                )
            else:
                lines.append(
                    f"✅ @{name} ({phone}) | trust: <b>{trust:.2f}</b> | flood: {flood_cnt}"
                )

    kb = _back_kb()
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Bots health ────────────────────────────────────────────────────────────────

async def _check_bot_alive(token: str, http_session=None) -> tuple[bool, str]:
    """Check if a bot token is valid via Bot API /getMe."""
    import aiohttp
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    username = data["result"].get("username", "")
                    return True, username
                return False, ""
    except Exception:
        return False, ""


@router.callback_query(HealthCb.filter(F.action == "bots_health"))
async def cb_health_bots(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    bots = await pool.fetch(
        """
        SELECT b.bot_id, b.username, b.first_name, b.token,
               COALESCE(aud.cnt, 0) AS user_count
        FROM managed_bots b
        LEFT JOIN (
            SELECT bot_id, COUNT(*) AS cnt
            FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
        ) aud ON aud.bot_id = b.bot_id
        WHERE b.added_by=$1 AND b.is_active=TRUE
        ORDER BY b.added_at DESC
        """,
        user_id,
    )

    lines = ["🤖 <b>Здоровье ботов</b>\n"]
    if not bots:
        lines.append("Нет добавленных ботов.")
    else:
        tasks = [_check_bot_alive(b["token"]) for b in bots]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for bot_rec, result in zip(bots, results):
            name = bot_rec["username"] or bot_rec["first_name"] or f"id{bot_rec['bot_id']}"
            user_count = bot_rec["user_count"]
            if isinstance(result, Exception) or not result[0]:
                lines.append(f"❌ @{name} — токен недействителен")
            else:
                lines.append(
                    f"✅ @{name} — активен | {user_count:,} пользователей"
                )

    kb = _back_kb()
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Flood log ──────────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "flood_log"))
async def cb_flood_log(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """
            SELECT afl.operation, afl.flood_seconds, afl.created_at, ta.phone
            FROM account_flood_log afl
            JOIN tg_accounts ta ON ta.id = afl.account_id
            WHERE ta.owner_id=$1
            ORDER BY afl.created_at DESC
            LIMIT 15
            """,
            user_id,
        )
        table_available = True
    except Exception:
        rows = []
        table_available = False

    lines = ["🌊 <b>Flood log</b>\n"]
    if not table_available:
        lines.append("ℹ️ Таблица flood-событий ещё не создана.\nFlood-события будут записываться автоматически.")
    elif not rows:
        lines.append("Нет flood-событий за последнее время.")
    else:
        for row in rows:
            dt = row["created_at"].strftime("%m-%d %H:%M") if row["created_at"] else "—"
            phone = row["phone"] or "—"
            op = row["operation"] or "—"
            secs = row["flood_seconds"] or 0
            lines.append(f"<code>{dt}</code> | {phone} | {op} | ⏱ {secs}s")

    kb = _back_kb()
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )
