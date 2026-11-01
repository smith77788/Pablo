"""Еженедельный отчёт владельцу — сам приходит в бот по понедельникам.

ЗАЧЕМ. Пульс-дайджест (services.organism.digest) собирал цельную картину
«что изменилось и что делать», но отдавался только по запросу мини-аппа:
владелец, который не открыл приложение, не узнавал ни о росте, ни о падении.
У лидеров рынка сводка приходит сама. Здесь — раз в неделю, утром понедельника,
в личку: главное одной строкой, ключевые числа с изменением к прошлому отчёту,
заказы и выручка бота-продавца, три самых важных действия.

КОГДА. Окно — понедельник 06:00–12:00 UTC (09:00–15:00 МСК): сердцебиение
организма идёт раз в 15 минут, окно с запасом переживает рестарт. Повтор держит
`db.notify_dedup_ok` с ключом недели — переживает рестарт и не шлёт второй
отчёт за ту же неделю.

ВЫКЛЮЧИТЬ. Кнопка под отчётом — тот же «Больше не напоминать» организма
(SnoozeCb, список dismissed), поэтому возврат — кнопкой «Вернуть» или /unmute.

Тренды считаются от прошлого ОТЧЁТА (своя база в organism_state), а не от
последнего открытия экрана в мини-аппе.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

log = logging.getLogger(__name__)

REPORT_ID = "weekly_report"
_STATE_KEY = "digest_metrics_weekly"
_WINDOW_HOURS = (6, 12)
_DEDUP_S = 6 * 86400

_SALES_SQL = """
    SELECT currency,
           count(*) FILTER (WHERE created_at >= now() - INTERVAL '7 days') AS orders,
           count(*) FILTER (WHERE created_at >= now() - INTERVAL '7 days'
                            AND status IN ('confirmed','done')) AS paid,
           COALESCE(sum(total_cents) FILTER (WHERE created_at >= now() - INTERVAL '7 days'
                            AND status IN ('confirmed','done')), 0) AS revenue,
           COALESCE(sum(total_cents) FILTER (WHERE created_at < now() - INTERVAL '7 days'
                            AND status IN ('confirmed','done')), 0) AS revenue_prev
      FROM bot_sales_orders
     WHERE owner_id = $1 AND created_at >= now() - INTERVAL '14 days'
     GROUP BY currency
"""

_CUR_SIGN = {"USD": "$", "RUB": "₽", "EUR": "€", "UAH": "₴", "KZT": "₸"}


def in_window(now: datetime) -> bool:
    """Понедельник, утро по UTC. ЧИСТАЯ."""
    return now.weekday() == 0 and _WINDOW_HOURS[0] <= now.hour < _WINDOW_HOURS[1]


def week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{REPORT_ID}:{iso[0]}-W{iso[1]:02d}"


def _money(cents: int, currency: str) -> str:
    value = f"{int(cents) / 100:,.0f}".replace(",", " ")
    sign = _CUR_SIGN.get((currency or "").upper())
    return f"{sign}{value}" if sign in ("$", "€") else f"{value} {sign or escape(currency or '')}"


def _delta(trend: dict | None) -> str:
    if not trend or not trend.get("delta"):
        return ""
    d = int(trend["delta"])
    mark = "🟢" if trend.get("good") else "🔴"
    return f" {mark} {'+' if d > 0 else '−'}{abs(d)}"


def format_report(digest: dict, sales: list[dict] | None = None) -> str:
    """Отчёт в HTML для Telegram. ЧИСТАЯ: на входе дайджест и строки продаж."""
    lines = ["📊 <b>Отчёт за неделю</b>", "", escape(digest.get("headline") or "")]

    if sales:
        lines += ["", "💰 <b>Продажи бота-продавца</b>"]
        for row in sales:
            cur = str(row.get("currency") or "USD")
            rev, prev = int(row.get("revenue") or 0), int(row.get("revenue_prev") or 0)
            trend = ""
            if prev:
                pct = round((rev - prev) * 100 / prev)
                trend = f" ({'+' if pct >= 0 else '−'}{abs(pct)}% к прошлой неделе)"
            lines.append(
                f"Заказов: <b>{int(row.get('orders') or 0)}</b>, оплачено: "
                f"<b>{int(row.get('paid') or 0)}</b>, выручка: <b>{_money(rev, cur)}</b>{trend}")

    for sec in digest.get("sections") or []:
        stats = [s for s in sec.get("stats") or [] if int(s.get("value") or 0)
                 or (s.get("trend") or {}).get("delta")]
        if not stats:
            continue
        lines += ["", f"<b>{escape(sec.get('title') or '')}</b>"]
        for s in stats:
            lines.append(f"{escape(s.get('label') or '')}: <b>{int(s.get('value') or 0)}</b>"
                         f"{_delta(s.get('trend'))}")

    recs = (digest.get("recommendations") or [])[:3]
    if recs:
        lines += ["", "👉 <b>Что сделать на этой неделе</b>"]
        for i, r in enumerate(recs, 1):
            why = f" — {escape(r['why'])}" if r.get("why") else ""
            lines.append(f"{i}. {escape(r.get('title') or '')}{why}")

    if not digest.get("has_prev"):
        lines += ["", "<i>Это первый отчёт: со следующей недели появятся изменения к прошлой.</i>"]
    return "\n".join(lines)


async def sales_week(pool, owner_id: int) -> list[dict]:
    """Заказы и выручка бота-продавца за 7 дней по валютам. Пусто — продаж нет."""
    try:
        rows = await pool.fetch(_SALES_SQL, owner_id)
    except Exception:
        log.debug("weekly_report: продажи не прочитаны owner=%s", owner_id, exc_info=True)
        return []
    return [dict(r) for r in rows if int(r["orders"] or 0) or int(r["revenue_prev"] or 0)]


def _kb():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import SnoozeCb

    kb = InlineKeyboardBuilder()
    kb.button(text="🔕 Не присылать отчёт",
              callback_data=SnoozeCb(action="off", sid=REPORT_ID, code="never"))
    return kb.as_markup()


async def maybe_send(pool, bot, owner_id: int, *, now: datetime | None = None) -> bool:
    """Отправить отчёт недели, если пора и ещё не отправлен. True — отправлен."""
    from database import db

    from services.organism import digest, spine

    now = now or datetime.now(UTC)
    if not in_window(now):
        return False
    try:
        dismissed = await spine.state_get(pool, owner_id, "dismissed", []) or []
    except Exception:
        dismissed = []
    if REPORT_ID in dismissed:
        return False
    key = week_key(now)
    if not await db.notify_dedup_ok(pool, owner_id, key, _DEDUP_S):
        return False
    try:
        report = await digest.build(pool, owner_id, state_key=_STATE_KEY)
        text = format_report(report, await sales_week(pool, owner_id))
        await bot.send_message(owner_id, text, parse_mode="HTML", reply_markup=_kb(),
                               disable_web_page_preview=True)
    except Exception:
        log.warning("weekly_report: не отправлен owner=%s", owner_id, exc_info=True)
        # Отметку недели снимаем: следующий такт в том же окне попробует снова.
        try:
            await db.notify_dedup_forget(pool, owner_id, key)
        except Exception:
            pass
        return False
    return True

