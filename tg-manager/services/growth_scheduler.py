"""Background task: запускает запланированные growth-посты каждые 10 минут.

Дополнительно каждые 24 часа:
- Drip-уведомления free-tier пользователям (раз в 7 дней на пользователя)
- Автопост статистики в канал BotMother (раз в 7 дней)
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot

from database import db
from services import account_manager

log = logging.getLogger(__name__)

_INTERVAL = 600          # 10 минут — тик growth schedules
_DRIP_INTERVAL = 86400   # 24 часа — тик drip + channel promo
_UPSELL_COOLDOWN_DAYS = 7  # не чаще раза в 7 дней на пользователя

# Варианты upsell-сообщений — ротация чтобы не приедалось
_UPSELL_MESSAGES = [
    (
        "💡 <b>Ты используешь BotMother бесплатно</b>\n\n"
        "С подпиской открываются:\n"
        "• DM-кампании — персональные сообщения тысячам\n"
        "• Публикация во все каналы одной кнопкой\n"
        "• AI-ассистент для контента и аналитики\n"
        "• Парсер аудитории из любых Telegram-каналов\n\n"
        "💎 <b>Подписка — $29/мес</b>\n"
        "👉 /subscription"
    ),
    (
        "🚀 <b>Рост Telegram без ручного труда</b>\n\n"
        "Пользователи BotMother с подпиской:\n"
        "• Публикуют во 100+ каналов автоматически\n"
        "• Отправляют тысячи DM в день\n"
        "• Управляют сетью ботов из одного места\n"
        "• Парсят и анализируют аудиторию\n\n"
        "💎 <b>Попробуй платные функции — $29/мес</b>\n"
        "👉 /subscription"
    ),
    (
        "⚡ <b>Что ты теряешь на бесплатном плане</b>\n\n"
        "• Нельзя запускать DM-кампании\n"
        "• Нельзя публиковать массово в каналы\n"
        "• Нет парсера аудитории\n"
        "• Нет AI-ассистента\n"
        "• Лимит 5 ботов и 5 каналов\n\n"
        "💎 <b>Подписка снимает все ограничения — $29/мес</b>\n"
        "Оплата: TON или USDT TRC-20\n"
        "👉 /subscription"
    ),
    (
        "📊 <b>BotMother работает прямо сейчас</b>\n\n"
        "Пока ты читаешь это — другие пользователи:\n"
        "• Публикуют посты в сотни каналов\n"
        "• Рассылают DM тысячам пользователей\n"
        "• Парсят аудиторию конкурентов\n"
        "• Автоматизируют весь Telegram-маркетинг\n\n"
        "💎 <b>Присоединяйся — $29/мес</b>\n"
        "👉 /subscription"
    ),
]


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    _last_drip = 0.0
    while True:
        try:
            await _tick(pool, bot)
        except Exception as exc:
            log.exception("growth_scheduler tick error: %s", exc)

        # Drip и channel promo раз в 24 часа
        import time
        now = time.monotonic()
        if now - _last_drip >= _DRIP_INTERVAL:
            _last_drip = now
            try:
                await _drip_upsell(pool, bot)
            except Exception as exc:
                log.exception("growth_scheduler drip error: %s", exc)
            try:
                await _maybe_post_channel_promo(pool, bot)
            except Exception as exc:
                log.exception("growth_scheduler channel_promo error: %s", exc)

        await asyncio.sleep(_INTERVAL)


async def _tick(pool: asyncpg.Pool, bot: Bot) -> None:
    try:
        schedules = await db.get_due_growth_schedules(pool)
    except Exception:
        return  # таблицы ещё не созданы

    for sched in schedules:
        try:
            await _fire(pool, bot, sched)
        except Exception as exc:
            log.warning("growth_scheduler fire sched=%s: %s", sched["id"], exc)


async def _fire(pool: asyncpg.Pool, bot: Bot, sched: dict) -> None:
    user_id = sched["user_id"]
    channels = await db.get_managed_channels(pool, user_id)
    if not channels:
        await db.advance_growth_schedule(pool, sched["id"], sched["interval_h"], 0)
        return

    # Подставляем реальную статистику в шаблон
    try:
        stats = await db.get_growth_platform_stats(pool)
        me = await bot.get_me()
        code = await db.get_or_create_referral_code(pool, user_id)
        ref_link = f"https://t.me/{me.username}?start={code}"
    except Exception:
        ref_link = "https://t.me/BotMotherBot"
        stats = {"total_users": 0, "total_ops": 0, "total_channels": 0, "week_users": 0}

    content = sched["template"].format(
        users=f"{int(stats['total_users']):,}".replace(",", " "),
        ops=f"{int(stats['total_ops']):,}".replace(",", " "),
        channels=f"{int(stats['total_channels']):,}".replace(",", " "),
        ref_link=ref_link,
    )

    # Брендинг: free-tier → всегда @MEXAHI3MBOT; paid → watermark если включён
    try:
        from services import brand_injection as _bi
        if await _bi.is_user_free_tier(pool, user_id):
            content = _bi.add_promo(content, html=True, context="channel")
        else:
            settings = await db.get_growth_settings(pool, user_id)
            if settings.get("watermark_enabled"):
                content += f'\n\n📤 <a href="{ref_link}">BotMother</a>'
    except Exception:
        pass

    sent = failed = 0
    for ch in channels:
        try:
            acc_row = await db.get_account_for_telethon(pool, ch["acc_id"], user_id)
            if not acc_row or not acc_row["session_str"]:
                failed += 1
                continue
            res = await account_manager.post_to_channel(
                session_string=acc_row["session_str"],
                channel_id=ch["channel_id"],
                text=content,
                access_hash=ch.get("access_hash") or 0,
                _acc=dict(acc_row),
            )
            if res.get("error"):
                failed += 1
            else:
                sent += 1
        except Exception as exc:
            log.warning("growth_scheduler channel=%s: %s", ch.get("channel_id"), exc)
            failed += 1
        await asyncio.sleep(1.5)

    await db.advance_growth_schedule(pool, sched["id"], sched["interval_h"], sent)
    log.info("growth_scheduler sched=%s fired: sent=%d failed=%d", sched["id"], sent, failed)

    if sent > 0:
        try:
            await bot.send_message(
                user_id,
                f"🌱 <b>Авто-пост опубликован!</b>\n\n"
                f"📢 Каналов: <b>{sent}</b> ⚠️ Ошибок: <b>{failed}</b>\n"
                f"Следующий запуск через <b>{sched['interval_h']}ч</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── Drip-уведомления для free-tier ───────────────────────────────────────────

async def _drip_upsell(pool: asyncpg.Pool, bot: Bot) -> None:
    """Отправить upsell-nudge free-tier пользователям раз в 7 дней."""
    try:
        rows = await pool.fetch(
            """SELECT pu.user_id
               FROM platform_users pu
               WHERE pu.current_plan = 'free'
                 AND NOT COALESCE(pu.is_banned, false)
                 AND (pu.last_upsell_at IS NULL
                      OR pu.last_upsell_at < NOW() - INTERVAL '7 days')
                 AND pu.last_seen >= NOW() - INTERVAL '30 days'
               LIMIT 50"""
        )
    except Exception as exc:
        log.debug("drip_upsell: DB error: %s", exc)
        return

    if not rows:
        return

    msg = random.choice(_UPSELL_MESSAGES)
    sent = 0
    for row in rows:
        uid = row["user_id"]
        try:
            await bot.send_message(uid, msg, parse_mode="HTML")
            await pool.execute(
                "UPDATE platform_users SET last_upsell_at=NOW() WHERE user_id=$1", uid
            )
            sent += 1
        except Exception:
            pass  # пользователь заблокировал бота — пропускаем
        await asyncio.sleep(0.05)  # 20 сообщений/сек, не более

    if sent:
        log.info("drip_upsell: sent %d upsell messages", sent)


# ── Автопост в канал BotMother ────────────────────────────────────────────────

_CHANNEL_PROMO_INTERVAL_DAYS = 3  # промо-пост раз в 3 дня

async def _maybe_post_channel_promo(pool: asyncpg.Pool, bot: Bot) -> None:
    """Раз в 3 дня публикует ротирующий промо-пост о возможностях BotMother."""
    try:
        last_str = await pool.fetchval(
            "SELECT value FROM platform_settings WHERE key='bm_channel_last_promo'"
        )
        if last_str:
            last_dt = datetime.fromisoformat(last_str)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            delta = (datetime.now(timezone.utc) - last_dt).days
            if delta < _CHANNEL_PROMO_INTERVAL_DAYS:
                return
    except Exception:
        pass

    from services import botmother_channel as _bmc
    ok = await _bmc.post_promo(pool, bot)
    if ok:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            await pool.execute(
                """INSERT INTO platform_settings (key, value, updated_at)
                   VALUES ('bm_channel_last_promo', $1, NOW())
                   ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
                now_iso,
            )
        except Exception:
            pass
        log.info("growth_scheduler: posted promo to BotMother channel")
