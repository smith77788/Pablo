"""Восстановление подписок из платёжных провайдеров (после потери БД).

ЗАЧЕМ. Инцидент 2026-08-30 стёр базу, но история платежей хранится НЕ у нас:
CryptoBot/CryptoPay (метод API getInvoices) и Telegram Stars (Bot API
getStarTransactions) держат все оплаты у себя. По ним можно восстановить самое
важное — КТО платил, какой план и до какой даты, — даже без старой базы.

Ключевой момент: срок подписки считается от ДАТЫ ПЛАТЕЖА, а не от «сегодня».
Платёж трёхмесячной давности за 6 месяцев истекает через 3 месяца, а не через 6
от текущего дня. Поэтому здесь своя дата-привязанная логика (compute_expiries),
а не billing.activate_subscription (та продлевает от now()).

Безопасность: reconcile по умолчанию dry-run — только считает и показывает, ничего
не пишет, пока явно не запросят apply. Токены/ключи не логируются.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

_CRYPTOPAY_API = "https://pay.crypt.bot/api"


@dataclass(frozen=True)
class Payment:
    user_id: int
    plan: str
    months: int
    paid_at: datetime
    source: str          # 'cryptopay' | 'stars'
    ref: str             # invoice_id / transaction id (для отчёта/дедупа)


def parse_payload(payload: str) -> Optional[tuple[int, str, int]]:
    """Разобрать payload платежа 'user_id:plan:months'. None если формат чужой."""
    if not payload or ":" not in payload:
        return None
    parts = payload.split(":")
    if len(parts) < 3:
        return None
    try:
        uid = int(parts[0])
        plan = parts[1].strip()
        months = max(1, int(parts[2]))
    except (ValueError, TypeError):
        return None
    if uid <= 0 or not plan:
        return None
    return uid, plan, months


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_expiries(payments: list[Payment], *, now: Optional[datetime] = None
                     ) -> dict[int, dict]:
    """Проиграть платежи по времени и посчитать актуальный срок каждой подписки.

    Логика повторяет боевой upsert, но привязана к датам платежей: каждый платёж
    продлевает от текущего срока, если на момент платежа подписка ещё активна,
    иначе стартует от даты самого платежа. Возвращает {user_id: {plan, expires_at,
    months_total, payments}} только для тех, у кого итог ещё не истёк (expires_at >
    now). Месяц ≈ 30 дней (чуть в пользу клиента).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    ordered = sorted(payments, key=lambda p: _as_utc(p.paid_at))
    state: dict[int, dict] = {}
    for p in ordered:
        paid = _as_utc(p.paid_at)
        st = state.get(p.user_id)
        if st and st["expires_at"] > paid:
            base = st["expires_at"]          # ещё активна — продлеваем от срока
            months_total = st["months_total"] + p.months
        else:
            base = paid                       # истекла/новая — от даты платежа
            months_total = p.months
        state[p.user_id] = {
            "plan": p.plan,                  # план последнего платежа
            "expires_at": base + timedelta(days=30 * p.months),
            "months_total": months_total,
            "payments": (st["payments"] + 1) if st else 1,
        }
    # Только ещё действующие подписки.
    return {uid: s for uid, s in state.items() if s["expires_at"] > now}


# ── Забор истории у провайдеров (best-effort, fail-soft) ───────────────────────
async def fetch_cryptopay(http, token: str) -> list[Payment]:
    """Все ОПЛАЧЕННЫЕ счета CryptoBot/CryptoPay. Пусто при отсутствии токена/сбое."""
    if not token:
        return []
    out: list[Payment] = []
    offset = 0
    try:
        while True:
            async with http.get(
                f"{_CRYPTOPAY_API}/getInvoices",
                headers={"Crypto-Pay-API-Token": token},
                params={"status": "paid", "count": 1000, "offset": offset},
                timeout=30,
            ) as resp:
                data = await resp.json()
            if not data.get("ok"):
                log.warning("cryptopay getInvoices: ok=false")
                break
            items = (data.get("result") or {}).get("items") or []
            if not items:
                break
            for inv in items:
                parsed = parse_payload(inv.get("payload") or "")
                if not parsed:
                    continue
                uid, plan, months = parsed
                paid_raw = inv.get("paid_at") or inv.get("created_at")
                try:
                    paid_at = datetime.fromisoformat(str(paid_raw).replace("Z", "+00:00"))
                except Exception:
                    paid_at = datetime.now(timezone.utc)
                out.append(Payment(uid, plan, months, _as_utc(paid_at),
                                    "cryptopay", str(inv.get("invoice_id", "?"))))
            if len(items) < 1000:
                break
            offset += len(items)
    except Exception:
        log.warning("cryptopay: забор истории упал", exc_info=True)
    return out


async def fetch_stars(bot) -> list[Payment]:
    """Все звёздные платежи бота (Telegram Stars). Пусто при сбое/недоступности."""
    out: list[Payment] = []
    offset = 0
    try:
        while True:
            res = await bot.get_star_transactions(offset=offset, limit=100)
            txs = getattr(res, "transactions", None) or []
            if not txs:
                break
            for t in txs:
                src = getattr(t, "source", None)
                payload = getattr(src, "invoice_payload", None) if src else None
                if not payload:
                    continue  # не входящая оплата пользователя
                parsed = parse_payload(payload)
                if not parsed:
                    continue
                uid, plan, months = parsed
                date = getattr(t, "date", None) or datetime.now(timezone.utc)
                out.append(Payment(uid, plan, months, _as_utc(date),
                                   "stars", str(getattr(t, "id", "?"))))
            if len(txs) < 100:
                break
            offset += len(txs)
    except Exception:
        log.warning("stars: забор истории упал", exc_info=True)
    return out


# ── Применение к БД ────────────────────────────────────────────────────────────
_RESTORE_SUB = """
    INSERT INTO subscriptions(user_id, plan, expires_at, is_active, started_at)
    VALUES($1, $2, $3, true, now())
    ON CONFLICT(user_id) DO UPDATE
    SET plan       = EXCLUDED.plan,
        is_active  = true,
        expires_at = GREATEST(subscriptions.expires_at, EXCLUDED.expires_at)
"""


async def reconcile(pool, bot, http, *, dry_run: bool = True) -> dict:
    """Собрать платежи, посчитать сроки и (если не dry-run) восстановить подписки.

    Возвращает отчёт: {found, users, active, applied, by_source, sample, dry_run}.
    По умолчанию dry_run=True — НИЧЕГО не пишет, только считает.
    """
    token = os.getenv("CRYPTOPAY_TOKEN", "")
    payments: list[Payment] = []
    payments += await fetch_cryptopay(http, token)
    payments += await fetch_stars(bot)

    by_source: dict[str, int] = {}
    for p in payments:
        by_source[p.source] = by_source.get(p.source, 0) + 1

    expiries = compute_expiries(payments)
    report = {
        "found": len(payments),
        "by_source": by_source,
        "users": len(expiries),
        "active": len(expiries),
        "applied": 0,
        "dry_run": dry_run,
        "sample": [],
    }
    # Показать первые несколько для проверки глазами.
    for uid, s in list(expiries.items())[:10]:
        report["sample"].append({
            "user_id": uid, "plan": s["plan"],
            "expires_at": s["expires_at"].isoformat(),
            "payments": s["payments"],
        })

    if dry_run:
        return report

    applied = 0
    async with pool.acquire() as conn:
        for uid, s in expiries.items():
            try:
                await conn.execute(_RESTORE_SUB, uid, s["plan"], s["expires_at"])
                # platform_users — только если строка уже есть (не плодим полупустые);
                # новый пользователь получит строку при первом заходе, подписка уже в силе.
                await conn.execute(
                    "UPDATE platform_users SET current_plan=$2, plan_expires_at=$3 "
                    "WHERE user_id=$1", uid, s["plan"], s["expires_at"])
                applied += 1
            except Exception:
                log.warning("reconcile: не удалось восстановить user=%s", uid, exc_info=True)
    report["applied"] = applied
    log.info("payment_recovery: восстановлено подписок %d из %d платящих",
             applied, len(expiries))
    return report
