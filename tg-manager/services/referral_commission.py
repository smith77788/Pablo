"""Процент рефереру с каждой оплаты приглашённого.

ЗАЧЕМ. Реферальная программа давала награду только за пороги («3 платящих —
30 дней») и один раз: `mark_referral_paid` возвращает реферера лишь при ПЕРВОЙ
оплате приглашённого, и продления больше ничего не приносили. У лидеров
партнёр получает долю с каждого платежа, пока клиент платит. Здесь — днями
подписки: `REFERRAL_COMMISSION_PCT` (по умолчанию 20 %) от оплаченного срока
приглашённого, с каждой подтверждённой оплаты.

КАК. `credit_in_tx` вызывается внутри транзакции подтверждения платежа (крипта
— payment_checker, Stars — subscription.confirm_stars_payment), во вложенной
точке сохранения: сбой начисления не откатывает саму оплату. Повтор того же
платежа ничего не добавит — `referral_commissions.payment_ref` уникален.
Уведомление — `notify` после фиксации транзакции.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_PCT = 20
DAYS_PER_MONTH = 30


def commission_pct() -> int:
    """Доля рефереру в процентах (env REFERRAL_COMMISSION_PCT, 0 — выключено)."""
    raw = (os.getenv("REFERRAL_COMMISSION_PCT") or "").strip()
    if not raw:
        return _DEFAULT_PCT
    try:
        return max(0, min(50, int(raw)))
    except ValueError:
        log.warning("referral_commission: bad REFERRAL_COMMISSION_PCT=%r", raw)
        return _DEFAULT_PCT


def commission_days(paid_months: int, pct: int | None = None) -> int:
    """Дней подписки рефереру за оплату `paid_months` месяцев. ЧИСТАЯ.

    Округление вверх до дня: 20 % от месяца — 6 дней, от года — 72 дня.
    """
    pct = commission_pct() if pct is None else pct
    months = int(paid_months or 0)
    if pct <= 0 or months < 1:
        return 0
    return -(-months * DAYS_PER_MONTH * pct // 100)


async def credit_in_tx(conn, referred_id: int, payment_ref: str,
                       paid_months: int) -> tuple[int, int] | None:
    """Начислить рефереру дни за этот платёж. (referrer_id, days) или None.

    `conn` — соединение с открытой транзакцией подтверждения платежа.
    """
    days = commission_days(paid_months)
    if not days or not payment_ref:
        return None
    from services import billing

    try:
        async with conn.transaction():  # точка сохранения
            referrer_id = await conn.fetchval(
                "SELECT referrer_id FROM platform_referrals WHERE referred_id=$1 "
                "AND referrer_id <> referred_id ORDER BY created_at LIMIT 1",
                referred_id)
            if not referrer_id:
                return None
            claimed = await conn.fetchval(
                """INSERT INTO referral_commissions
                       (referrer_id, referred_id, payment_ref, paid_months, days)
                   VALUES($1, $2, $3, $4, $5)
                   ON CONFLICT (payment_ref) DO NOTHING RETURNING id""",
                referrer_id, referred_id, payment_ref, int(paid_months), days)
            if not claimed:
                return None
            await billing.extend_days(conn, int(referrer_id), days)
            return int(referrer_id), days
    except Exception:
        log.warning("referral_commission: не начислено referred=%s ref=%s",
                    referred_id, payment_ref, exc_info=True)
        return None


async def notify(bot, credited: tuple[int, int] | None) -> None:
    if not credited or bot is None:
        return
    referrer_id, days = credited
    try:
        await bot.send_message(
            referrer_id,
            f"💸 <b>+{days} дн. подписки</b>\n\n"
            f"Приглашённый вами человек оплатил подписку — ваша доля "
            f"{commission_pct()} % от его срока уже добавлена к вашей.\n"
            f"/referral — все начисления",
            parse_mode="HTML")
    except Exception:
        log.debug("referral_commission: уведомление не доставлено referrer=%s",
                  referrer_id, exc_info=True)


async def totals(pool, referrer_id: int) -> tuple[int, int]:
    """(число начислений, всего дней) — для экрана рефералов."""
    try:
        row = await pool.fetchrow(
            "SELECT count(*) AS n, COALESCE(sum(days), 0) AS d "
            "FROM referral_commissions WHERE referrer_id=$1", referrer_id)
    except Exception:
        return 0, 0
    return (int(row["n"]), int(row["d"])) if row else (0, 0)
