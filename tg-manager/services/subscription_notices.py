"""Напоминания владельцу о сроке подписки.

ЗАЧЕМ. `scheduler.expire_stale_subscriptions` молча гасил истёкшие подписки:
человек узнавал о конце подписки, когда операции начинали отказывать «доступно
по подписке», а прогрев вставал. Теперь владелец получает два сообщения:
за несколько дней до конца и в момент, когда подписка закончилась, — оба с
кнопкой продления.

Повтор держит `db.notify_dedup_ok` (переживает рестарт). Ключ содержит дату
окончания: продлил подписку — у новой даты свой ключ, и следующее напоминание
придёт уже про неё.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# За сколько дней до конца напоминать.
REMIND_DAYS = 3
# Сколько после окончания ещё сообщаем «подписка закончилась» (на случай, если
# процесс лежал в сам момент окончания).
EXPIRED_WINDOW_DAYS = 2
# Анти-повтор: одна дата окончания — одно сообщение каждого вида.
_DEDUP_S = 30 * 86400


def _renew_kb():
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from bot.callbacks import SubCb

    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Продлить подписку", callback_data=SubCb(action="choose_plan", plan="paid"))
    return kb.as_markup()


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def expiring_text(expires_at: datetime, now: datetime) -> str:
    exp = _as_utc(expires_at)
    left = exp - now
    days = left.days
    if days >= 1:
        when = f"через {days} дн."
    else:
        hours = max(1, int(left.total_seconds() // 3600))
        when = f"через {hours} ч"
    return (
        "⏳ <b>Подписка скоро закончится</b>\n\n"
        f"Действует до <b>{exp.strftime('%d.%m.%Y %H:%M')}</b> UTC — {when}.\n\n"
        "После окончания встанут прогрев аккаунтов и операции, доступные по "
        "подписке. Продлите заранее — новый срок добавится к текущему."
    )


def expired_text(expires_at: datetime) -> str:
    exp = _as_utc(expires_at)
    return (
        "🔒 <b>Подписка закончилась</b>\n\n"
        f"Срок истёк {exp.strftime('%d.%m.%Y %H:%M')} UTC. Прогрев аккаунтов "
        "приостановлен, операции по подписке недоступны.\n\n"
        "Планы прогрева и настройки сохранены: после продления всё продолжится "
        "с того же места."
    )


async def _send_once(pool, bot, user_id: int, key: str, text: str) -> bool:
    from database import db

    if not await db.notify_dedup_ok(pool, user_id, key, _DEDUP_S):
        return False
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=_renew_kb())
        return True
    except Exception:
        # Отметку не снимаем: чаще всего бот заблокирован пользователем, и
        # повтор каждый час ничего не даст, кроме шума в логе.
        log_exc_swallow(log, "subscription_notices: сообщение не доставлено",
                        user_id=user_id)
        return False


async def run_once(pool: asyncpg.Pool, bot) -> dict:
    """Один проход: напомнить о скором конце и сообщить о закончившихся."""
    from bot.utils.subscription import is_platform_admin

    now = datetime.now(timezone.utc)
    sent = {"expiring": 0, "expired": 0}
    try:
        expiring = await pool.fetch(
            "SELECT user_id, expires_at FROM subscriptions "
            "WHERE is_active = true AND plan <> 'free' AND expires_at > now() "
            "AND expires_at <= now() + make_interval(days => $1::int)",
            REMIND_DAYS,
        )
        expired = await pool.fetch(
            "SELECT user_id, expires_at FROM subscriptions "
            "WHERE plan <> 'free' AND expires_at <= now() "
            "AND expires_at > now() - make_interval(days => $1::int)",
            EXPIRED_WINDOW_DAYS,
        )
    except Exception:
        log_exc_swallow(log, "subscription_notices: выборка подписок не удалась")
        return sent

    for r in expiring:
        uid = int(r["user_id"])
        if is_platform_admin(uid):
            continue
        key = f"sub_expiring:{_as_utc(r['expires_at']).date().isoformat()}"
        if await _send_once(pool, bot, uid, key, expiring_text(r["expires_at"], now)):
            sent["expiring"] += 1
    for r in expired:
        uid = int(r["user_id"])
        if is_platform_admin(uid):
            continue
        key = f"sub_expired:{_as_utc(r['expires_at']).date().isoformat()}"
        if await _send_once(pool, bot, uid, key, expired_text(r["expires_at"])):
            sent["expired"] += 1
    if sent["expiring"] or sent["expired"]:
        log.info("subscription_notices: %s", sent)
    return sent
