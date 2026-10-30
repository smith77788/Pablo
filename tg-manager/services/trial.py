"""Пробный период: путь новичка до первого результата без оплаты (D15).

На бесплатном тарифе нельзя запустить ни одной операции (все OP_REGISTRY —
`min_plan`), поэтому новичок, подключивший аккаунт, упирался в пейволл раньше,
чем видел, что продукт вообще работает. Решение владельца продукта: после
первого подключённого аккаунта даётся TRIAL_DAYS дней платного тарифа, один раз.

КОГДА ВЫДАЁТСЯ. Не в момент записи аккаунта (входов в базу несколько, и
пропустить один — значит молча не дать триал), а в первый момент, когда он
нужен: мини-апп/бот рисуют чеклист онбординга или шина операций собирается
отказать по тарифу. Условие одно — у владельца есть живой аккаунт с сессией.
Отсчёт идёт с выдачи, а не с подключения: человек, подключивший аккаунт и
вернувшийся через неделю, не находит триал уже сгоревшим.

ОДИН РАЗ. Не выдаётся, если у владельца уже была любая подписка (строка в
`subscriptions` — и оплаченная, и прошлый триал), ручная выдача админом, или
если хоть один его Telegram-аккаунт уже приносил триал другому владельцу
(второй бот-пользователь с теми же аккаунтами второй триал не получит).
Запись в `subscription_trials` — это и защита, и след: кто, когда, до какого
числа и с какими аккаунтами.

Подписка триала — обычная строка `subscriptions` (plan='paid'): её читают все
гейты, её же продлевает оплата (оставшиеся дни триала не сгорают), о её конце
предупреждает общее напоминание.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from services.account_status import DEAD_STATUSES

log = logging.getLogger(__name__)

# Мёртвые статусы — единый словарь account_status (не пересказываем).
_DEAD = tuple(sorted(DEAD_STATUSES))


def trial_days() -> int:
    """Длина триала в днях (env TRIAL_DAYS, по умолчанию 3; 0 — триал выключен)."""
    raw = (os.getenv("TRIAL_DAYS") or "").strip()
    if not raw:
        return 3
    try:
        return max(0, min(30, int(raw)))
    except ValueError:
        log.warning("trial: bad TRIAL_DAYS=%r — using 3", raw)
        return 3


async def is_eligible(pool, owner_id: int, *, need_account: bool = True) -> bool:
    """Может ли владелец получить триал прямо сейчас.

    need_account=False — проверка «триал ещё не использован» без требования
    живого аккаунта: так бот решает, пускать ли бесплатного новичка подключить
    первый аккаунт (лимит бесплатного тарифа — 0).
    """
    if trial_days() <= 0:
        return False
    row = await pool.fetchrow(
        """
        SELECT
          EXISTS (SELECT 1 FROM subscription_trials WHERE owner_id=$1) AS had_trial,
          EXISTS (SELECT 1 FROM subscriptions WHERE user_id=$1) AS had_sub,
          EXISTS (SELECT 1 FROM platform_users WHERE user_id=$1
                  AND COALESCE(current_plan, 'free') NOT IN ('free', '')) AS had_grant,
          EXISTS (
            SELECT 1 FROM subscription_trials t
            WHERE t.owner_id <> $1 AND (
              $1 = ANY(t.account_tg_user_ids)
              OR EXISTS (SELECT 1 FROM tg_accounts a
                         WHERE a.owner_id=$1 AND a.tg_user_id IS NOT NULL
                           AND (a.tg_user_id = ANY(t.account_tg_user_ids)
                                OR a.tg_user_id = t.owner_id)))
          ) AS reused,
          EXISTS (
            SELECT 1 FROM tg_accounts WHERE owner_id=$1
              AND session_str IS NOT NULL AND session_str <> ''
              AND is_active IS NOT FALSE
              AND COALESCE(acc_status, 'active') <> ALL($2::text[])
          ) AS has_alive
        """,
        owner_id, list(_DEAD))
    if not row or row["had_trial"] or row["had_sub"] or row["had_grant"] or row["reused"]:
        return False
    return bool(row["has_alive"]) or not need_account


async def maybe_grant(pool, owner_id: int) -> Optional[datetime]:
    """Выдать триал, если положен. Возвращает срок окончания или None.

    Идемпотентно и безопасно при гонке: строка триала и строка подписки
    пишутся одной транзакцией, обе с ON CONFLICT DO NOTHING — второй
    одновременный вызов не выдаёт ничего.
    """
    days = trial_days()
    if days <= 0:
        return None
    try:
        if not await is_eligible(pool, owner_id):
            return None
        async with pool.acquire() as conn:
            async with conn.transaction():
                tg_ids = [int(r["tg_user_id"]) for r in await conn.fetch(
                    "SELECT DISTINCT tg_user_id FROM tg_accounts "
                    "WHERE owner_id=$1 AND tg_user_id IS NOT NULL", owner_id)]
                expires = await conn.fetchval(
                    "INSERT INTO subscription_trials(owner_id, granted_at, expires_at, "
                    "days, account_tg_user_ids) "
                    "VALUES ($1, now(), now() + make_interval(days => $2), $2, $3::bigint[]) "
                    "ON CONFLICT (owner_id) DO NOTHING RETURNING expires_at",
                    owner_id, days, tg_ids)
                if expires is None:
                    return None
                sub = await conn.fetchval(
                    "INSERT INTO subscriptions(user_id, plan, expires_at, is_active) "
                    "VALUES ($1, 'paid', $2, true) "
                    "ON CONFLICT (user_id) DO NOTHING RETURNING expires_at",
                    owner_id, expires)
                if sub is None:
                    # Подписка появилась между проверкой и записью — триал не нужен.
                    raise _AlreadySubscribed()
    except _AlreadySubscribed:
        return None
    except Exception:
        # Триал — подарок, а не дверь: его сбой не должен ломать экран или
        # операцию. Человек увидит обычный пейволл.
        log.warning("trial: grant failed owner=%s", owner_id, exc_info=True)
        return None
    try:
        from bot.utils.subscription import invalidate_plan_cache
        invalidate_plan_cache(owner_id)
    except Exception:
        log.warning("trial: plan cache invalidation failed", exc_info=True)
    log.info("trial: granted owner=%s days=%s until=%s", owner_id, days, expires)
    return expires


class _AlreadySubscribed(Exception):
    pass


async def active_trial(pool, owner_id: int) -> Optional[dict]:
    """Идущий триал владельца: {expires_at, days_left} или None (триала нет,
    кончился или поверх него уже оплачена подписка)."""
    row = await pool.fetchrow(
        "SELECT t.expires_at, s.expires_at AS sub_expires "
        "FROM subscription_trials t "
        "LEFT JOIN subscriptions s ON s.user_id=t.owner_id AND s.is_active "
        "WHERE t.owner_id=$1 AND t.expires_at > now()", owner_id)
    if not row:
        return None
    if row["sub_expires"] is not None and row["sub_expires"] > row["expires_at"]:
        return None  # оплатил поверх триала — это уже подписка
    left = (row["expires_at"] - datetime.now(row["expires_at"].tzinfo)).total_seconds()
    return {"expires_at": row["expires_at"],
            "days_left": max(1, int((left + 86399) // 86400))}
