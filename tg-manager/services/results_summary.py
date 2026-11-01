"""«Итоги»: результат владельца за период — деньги, лиды, ответы, охват.

Главный экран показывал состояние флота (аккаунты, давление, операции), а
владелец приходит с другим вопросом: «что мне это принесло за неделю». Здесь
собраны четыре числа результата и до трёх карточек «требует внимания» — тех
же, что на дашборде (`mini_app_dashboard.attention_items`), чтобы бот и
мини-апп не расходились в том, что считать проблемой.

Что считается:

* **Деньги** — заказы ИИ-менеджера продаж (`bot_sales_orders`) в статусах
  «подтверждён» и «выполнен», по валютам, плюс выигранные сделки CRM
  (`crm_deals`, этап `won`; валюты у сделки нет, поэтому отдельной суммой).
* **Лиды** — новые подписчики ботов владельца (`bot_users.first_seen`).
* **Ответы** — входящие сообщения людей в диалогах с ботами
  (`relay_messages.direction = 'in'`).
* **Охват** — доставленные сообщения рассылок ботов владельца
  (`broadcasts.sent_count`).

Каждое число считается отдельным запросом и отдельно глушится: упавшая
выборка даёт `None` («нет данных»), а не пустой экран. Ноль и «нет данных» —
разные ответы, и экран показывает их по-разному.

Скоуп — строго владелец: боты через `managed_bots.added_by`, заказы и сделки
через `owner_id`.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_DAYS = 90
ATTENTION_LIMIT = 3


def _clamp_days(days) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    return max(1, min(MAX_DAYS, d))


def _delta(cur, prev):
    """Изменение к прошлому периоду в процентах; None — сравнивать не с чем."""
    if cur is None or prev is None or not prev:
        return None
    return round((cur - prev) * 100.0 / prev)


async def _scalar_pair(pool, sql: str, uid: int, days: int, what: str):
    """(текущий период, прошлый период) одним запросом; (None, None) при сбое."""
    try:
        row = await pool.fetchrow(sql, uid, days)
    except Exception:
        log.warning("итоги: не удалось посчитать «%s»", what, exc_info=True)
        return None, None
    if not row:
        return 0, 0
    return int(row["cur"] or 0), int(row["prev"] or 0)


# Окно — `$2` дней от текущего момента; прошлый период — столько же дней до
# него. Интервал собирается make_interval, чтобы asyncpg биндил целое число,
# а не строку.
_LEADS_SQL = """
SELECT
  COUNT(*) FILTER (WHERE bu.first_seen >= now() - make_interval(days => $2)) AS cur,
  COUNT(*) FILTER (WHERE bu.first_seen <  now() - make_interval(days => $2)
                     AND bu.first_seen >= now() - make_interval(days => $2 * 2)) AS prev
FROM bot_users bu
JOIN managed_bots mb ON mb.bot_id = bu.bot_id
WHERE mb.added_by = $1
  AND bu.first_seen >= now() - make_interval(days => $2 * 2)
"""

_REPLIES_SQL = """
SELECT
  COUNT(*) FILTER (WHERE rm.created_at >= now() - make_interval(days => $2)) AS cur,
  COUNT(*) FILTER (WHERE rm.created_at <  now() - make_interval(days => $2)
                     AND rm.created_at >= now() - make_interval(days => $2 * 2)) AS prev
FROM relay_messages rm
JOIN relay_sessions rs ON rs.id = rm.session_id
JOIN managed_bots mb ON mb.bot_id = rs.bot_id
WHERE mb.added_by = $1
  AND rm.direction = 'in'
  AND rm.created_at >= now() - make_interval(days => $2 * 2)
"""

_REACH_SQL = """
SELECT
  COALESCE(SUM(b.sent_count) FILTER (
      WHERE b.created_at >= now() - make_interval(days => $2)), 0) AS cur,
  COALESCE(SUM(b.sent_count) FILTER (
      WHERE b.created_at <  now() - make_interval(days => $2)
        AND b.created_at >= now() - make_interval(days => $2 * 2)), 0) AS prev
FROM broadcasts b
JOIN managed_bots mb ON mb.bot_id = b.bot_id
WHERE mb.added_by = $1
  AND b.created_at >= now() - make_interval(days => $2 * 2)
"""

_ORDERS_SQL = """
SELECT currency,
  COALESCE(SUM(total_cents) FILTER (
      WHERE created_at >= now() - make_interval(days => $2)), 0) AS cur,
  COALESCE(SUM(total_cents) FILTER (
      WHERE created_at <  now() - make_interval(days => $2)), 0) AS prev,
  COUNT(*) FILTER (WHERE created_at >= now() - make_interval(days => $2)) AS n
FROM bot_sales_orders
WHERE owner_id = $1
  AND status IN ('confirmed', 'done')
  AND created_at >= now() - make_interval(days => $2 * 2)
GROUP BY currency
"""

_DEALS_SQL = """
SELECT COUNT(*) AS n, COALESCE(SUM(value), 0) AS total
FROM crm_deals
WHERE owner_id = $1 AND stage = 'won'
  AND updated_at >= now() - make_interval(days => $2)
"""


async def _money(pool, uid: int, days: int) -> dict | None:
    out: dict = {"orders": 0, "by_currency": {}, "prev_by_currency": {},
                 "deals_won": 0, "deals_value": 0.0}
    ok = False
    try:
        rows = await pool.fetch(_ORDERS_SQL, uid, days)
        ok = True
        for r in rows or []:
            cur = str(r["currency"] or "USD").upper()
            out["by_currency"][cur] = int(r["cur"] or 0)
            out["prev_by_currency"][cur] = int(r["prev"] or 0)
            out["orders"] += int(r["n"] or 0)
    except Exception:
        log.warning("итоги: не удалось посчитать заказы", exc_info=True)
    try:
        row = await pool.fetchrow(_DEALS_SQL, uid, days)
        ok = True
        if row:
            out["deals_won"] = int(row["n"] or 0)
            out["deals_value"] = float(row["total"] or 0)
    except Exception:
        log.warning("итоги: не удалось посчитать сделки", exc_info=True)
    return out if ok else None


async def compute(pool, uid: int, days: int = 7, *, with_attention: bool = True) -> dict:
    """Итоги владельца за `days` дней (1..90) и сравнение с прошлым периодом."""
    days = _clamp_days(days)
    leads = await _scalar_pair(pool, _LEADS_SQL, uid, days, "лиды")
    replies = await _scalar_pair(pool, _REPLIES_SQL, uid, days, "ответы")
    reach = await _scalar_pair(pool, _REACH_SQL, uid, days, "охват")
    money = await _money(pool, uid, days)

    def _metric(pair):
        cur, prev = pair
        return {"value": cur, "prev": prev, "delta_pct": _delta(cur, prev)}

    out = {
        "days": days,
        "money": money,
        "leads": _metric(leads),
        "replies": _metric(replies),
        "reach": _metric(reach),
        "attention": [],
        "attention_total": 0,
    }
    if with_attention:
        try:
            from services.mini_app_dashboard import attention_items

            att = await attention_items(pool, uid)
            items = list(att.get("items") or [])
            out["attention"] = items[:ATTENTION_LIMIT]
            out["attention_total"] = len(items)
        except Exception:
            log.warning("итоги: не удалось собрать «требует внимания»", exc_info=True)
    return out


# ── Текст для бота ─────────────────────────────────────────────────────────

_CUR_SIGN = {"USD": "$", "EUR": "€", "RUB": "₽", "USDT": "USDT", "TON": "TON",
             "XTR": "⭐"}


def fmt_num(n) -> str:
    """12345 → «12 345» (узкий неразрывный пробел не нужен: это Telegram)."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def fmt_money(money: dict | None) -> str:
    if money is None:
        return "нет данных"
    parts = []
    for cur, cents in sorted((money.get("by_currency") or {}).items(),
                             key=lambda kv: -kv[1]):
        if not cents:
            continue
        sign = _CUR_SIGN.get(cur, cur)
        val = fmt_num(round(cents / 100))
        parts.append(f"{val} {sign}" if len(sign) > 1 else f"{sign}{val}")
    if money.get("deals_won"):
        parts.append(f"сделки: {fmt_num(money.get('deals_value') or 0)}")
    return " · ".join(parts) if parts else "0"


def _fmt_metric(m: dict) -> str:
    if m.get("value") is None:
        return "нет данных"
    s = fmt_num(m["value"])
    d = m.get("delta_pct")
    if d is not None:
        s += f" ({'+' if d >= 0 else ''}{d}%)"
    return s


def bot_text(summary: dict) -> str:
    """Сводка «Итоги» для сообщения бота (HTML)."""
    days = summary.get("days", 7)
    lines = [
        f"📊 <b>Итоги за {days} дн.</b>",
        "",
        f"💰 Деньги: <b>{fmt_money(summary.get('money'))}</b>",
        f"🧲 Лиды: <b>{_fmt_metric(summary.get('leads') or {})}</b>",
        f"💬 Ответы: <b>{_fmt_metric(summary.get('replies') or {})}</b>",
        f"📣 Охват: <b>{_fmt_metric(summary.get('reach') or {})}</b>",
    ]
    att = summary.get("attention") or []
    if att:
        lines += ["", "<b>Требует внимания</b>"]
        ico = {"high": "🔴", "medium": "🟠", "info": "🔵"}
        from html import escape
        for it in att:
            lines.append(f"{ico.get(it.get('severity'), '•')} {escape(str(it.get('title') or ''))}")
        more = int(summary.get("attention_total") or 0) - len(att)
        if more > 0:
            lines.append(f"<i>и ещё {more} — в приложении</i>")
    else:
        lines += ["", "✅ Ничего не требует внимания."]
    return "\n".join(lines)


def one_line(summary: dict) -> str:
    """Одна строка для /start вернувшегося владельца."""
    return (
        f"За {summary.get('days', 7)} дн.: 💰 {fmt_money(summary.get('money'))}"
        f" · 🧲 {_fmt_metric(summary.get('leads') or {})}"
        f" · 💬 {_fmt_metric(summary.get('replies') or {})}"
        f" · 📣 {_fmt_metric(summary.get('reach') or {})}"
    )
