"""Прогноз покупок: RFM-сегменты клиентов и вероятность покупки за 30 дней.

ЗАЧЕМ. Продажи бота лежали заказами, но система не говорила, кто купит снова,
кто уходит и с кем из интересовавшихся стоит договорить. У лидеров (Klaviyo,
HubSpot) у каждого клиента есть прогноз и готовые сегменты «под угрозой
ухода», «лучшие клиенты», по которым сразу делают рассылку.

Как считается (честно: это правила по истории, а не обученная модель):
  • покупка — заказ бота-продавца в статусе confirmed/done или оплаченный
    (money_funnel.PAID_ORDER_STATUSES, paid_at);
  • R — дней с последней покупки, F — число покупок, M — сумма в основной
    валюте владельца; «свой ритм» клиента — медиана дней между покупками;
  • вероятность покупки за 30 дней: постоянный клиент в своём ритме — высокая и
    гаснет по мере просрочки ритма; разовый — гаснет с давностью; не купивший —
    по типу последней реплики (готов купить, спрашивал цену) и её свежести;
  • точность меряется на своих же данных (backtest): прогноз на дату 30 дней
    назад сверяется с тем, кто правда купил за эти 30 дней. Пока клиентов мало,
    экран так и пишет, а не показывает выдуманную точность.

Сегмент можно поставить тегом на карточки контактов — дальше это обычный
сегмент по тегу: рассылка, список, выгрузка (services/contacts_hub).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

log = logging.getLogger(__name__)

HORIZON_DAYS = 30
LEAD_WINDOW_DAYS = 30
MIN_BACKTEST = 10

SEGMENTS = (
    ("champions", "Лучшие клиенты", "Покупают часто и недавно — им новинки и ранний доступ."),
    ("loyal", "Постоянные", "Покупают в своём ритме — напомнить к сроку следующей покупки."),
    ("at_risk", "Под угрозой ухода", "Покупали не раз, но пропустили свой ритм — вернуть, пока не ушли."),
    ("new", "Новые", "Одна покупка за последний месяц — довести до второй."),
    ("sleeping", "Спящие", "Одна покупка давно — разбудить предложением."),
    ("hot_leads", "Горячие без покупки", "Спрашивали цену или хотели купить, но заказа нет."),
    ("leads", "Интересовались", "Писали боту за последний месяц, покупки нет."),
)
SEGMENT_TAGS = {
    "champions": "прогноз_лучшие", "loyal": "прогноз_постоянные",
    "at_risk": "прогноз_под_угрозой", "new": "прогноз_новые",
    "sleeping": "прогноз_спящие", "hot_leads": "прогноз_горячие",
    "leads": "прогноз_интересовались",
}
_LEAD_BASE = {"ready_to_buy": 0.5, "price": 0.3, "question": 0.15,
              "complaint": 0.05, "other": 0.08}


def segment_label(seg: str) -> str:
    return next((label for k, label, _ in SEGMENTS if k == seg), "")


def _days(a: datetime, b: datetime) -> float:
    return max(0.0, (a - b).total_seconds() / 86400.0)


def _clip(p: float) -> float:
    return round(min(0.95, max(0.01, p)), 2)


def score(customer: dict, now: datetime) -> dict:
    """Один клиент → {segment, probability, recency_days, frequency, rhythm_days}.

    customer: {dates: [datetime покупок], last_msg_at, last_intent}. ЧИСТАЯ."""
    dates = sorted(d for d in (customer.get("dates") or []) if d and d <= now)
    f = len(dates)
    last_msg = customer.get("last_msg_at")
    msg_days = _days(now, last_msg) if last_msg else None
    out = {"frequency": f, "recency_days": None, "rhythm_days": None}
    if f == 0:
        if msg_days is None or msg_days > LEAD_WINDOW_DAYS:
            out.update(segment=None, probability=0.0)
            return out
        base = _LEAD_BASE.get(customer.get("last_intent") or "other", 0.08)
        p = _clip(base * math.exp(-msg_days / 7.0))
        out.update(segment="hot_leads" if p >= 0.25 else "leads", probability=p)
        return out
    r = _days(now, dates[-1])
    out["recency_days"] = round(r, 1)
    fresh_talk = msg_days is not None and msg_days <= 7 and (last_msg > dates[-1])
    if f == 1:
        p = 0.25 * math.exp(-r / 45.0) + (0.15 if fresh_talk else 0.0)
        out.update(segment="new" if r <= 30 else "sleeping", probability=_clip(p))
        return out
    gaps = [_days(b, a) for a, b in zip(dates, dates[1:])]
    rhythm = max(3.0, median(gaps))
    out["rhythm_days"] = round(rhythm, 1)
    top = min(0.85, 0.55 + 0.1 * min(f - 2, 3))
    late = max(0.0, r - rhythm)
    p = top * math.exp(-late / rhythm) + (0.1 if fresh_talk else 0.0)
    # Неделя запаса поверх ритма: заказ «на день позже обычного» — не уход.
    if f >= 3 and r <= rhythm * 1.5 + 7:
        seg = "champions"
    elif r <= rhythm * 2 + 7:
        seg = "loyal"
    else:
        seg = "at_risk"
    out.update(segment=seg, probability=_clip(p))
    return out


def score_all(customers: list[dict], now: datetime) -> list[dict]:
    """Клиенты с прогнозом, сильнейшие сверху; без сегмента — отброшены. ЧИСТАЯ."""
    out = []
    for c in customers:
        s = score(c, now)
        if not s.get("segment"):
            continue
        out.append({**{k: c.get(k) for k in ("peer_user_id", "name", "username",
                                                "monetary_cents", "currency")}, **s})
    out.sort(key=lambda x: (-x["probability"], x.get("recency_days") or 0))
    return out


def backtest(customers: list[dict], now: datetime) -> dict:
    """Прогноз на дату 30 дней назад против того, кто правда купил. ЧИСТАЯ.

    Проверяются только те, у кого к той дате была покупка: реплик того времени
    в базе нет, а прогноз по ним без них был бы другим, чем на экране."""
    cut = now - timedelta(days=HORIZON_DAYS)
    rows = []
    for c in customers:
        before = [d for d in c.get("dates") or [] if d and d <= cut]
        if not before:
            continue
        s = score({"dates": before}, cut)
        bought = any(cut < d <= now for d in c.get("dates") or [] if d)
        rows.append((s["probability"], bought))
    out: dict = {"checked": len(rows), "enough": len(rows) >= MIN_BACKTEST}
    if not out["enough"]:
        return out
    rows.sort(key=lambda x: -x[0])
    k = max(1, len(rows) // 3)
    top, rest = rows[:k], rows[k:]
    rate = lambda xs: round(sum(1 for _, b in xs if b) / len(xs), 2) if xs else None  # noqa: E731
    out.update(
        predicted=round(sum(p for p, _ in rows) / len(rows), 2),
        actual=rate(rows), top_third_actual=rate(top), rest_actual=rate(rest))
    return out


def summarize(scored: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for s in scored:
        counts[s["segment"]] = counts.get(s["segment"], 0) + 1
    return [{"id": k, "label": label, "hint": hint, "count": counts.get(k, 0),
             "tag": SEGMENT_TAGS[k]} for k, label, hint in SEGMENTS]


# ── Выборка из базы ────────────────────────────────────────────────────────

def _as_list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


async def load_customers(pool, owner_id: int) -> list[dict]:
    """Покупатели и собеседники ботов-продавцов владельца."""
    from services.ai_inbox_assist import classify_text
    from services.money_funnel import PAID_ORDER_STATUSES

    uid = int(owner_id)
    orders = await pool.fetch(
        """SELECT customer_chat_id, customer_name, customer_username, total_cents,
                  currency, COALESCE(paid_at, created_at) AS at
             FROM bot_sales_orders
            WHERE owner_id=$1 AND (status = ANY($2::text[]) OR paid_at IS NOT NULL)""",
        uid, list(PAID_ORDER_STATUSES))
    dialogs = await pool.fetch(
        """SELECT customer_chat_id, last_at, history FROM bot_sales_dialogs
            WHERE owner_id=$1 AND last_at > now() - interval '120 days'""", uid)
    cur_count: dict[str, int] = {}
    for o in orders:
        cur_count[o["currency"] or "USD"] = cur_count.get(o["currency"] or "USD", 0) + 1
    main_cur = max(cur_count, key=cur_count.get) if cur_count else "USD"
    by: dict[int, dict] = {}

    def _slot(pid):
        return by.setdefault(int(pid), {"peer_user_id": int(pid), "name": "", "username": "",
                                        "dates": [], "monetary_cents": 0,
                                        "currency": main_cur, "last_msg_at": None,
                                        "last_intent": None})
    for o in orders:
        c = _slot(o["customer_chat_id"])
        c["dates"].append(o["at"])
        c["name"] = c["name"] or (o["customer_name"] or "")
        c["username"] = c["username"] or (o["customer_username"] or "")
        if (o["currency"] or "USD") == main_cur:
            c["monetary_cents"] += int(o["total_cents"] or 0)
    for d in dialogs:
        c = _slot(d["customer_chat_id"])
        if c["last_msg_at"] is None or (d["last_at"] and d["last_at"] > c["last_msg_at"]):
            c["last_msg_at"] = d["last_at"]
            last_user = next((str(t.get("content") or "") for t in reversed(_as_list(d["history"]))
                              if isinstance(t, dict) and t.get("role") == "user"), "")
            c["last_intent"] = classify_text(last_user)
    if by:
        names = await pool.fetch(
            "SELECT DISTINCT ON (user_id) user_id, first_name, username FROM bot_users "
            "WHERE user_id = ANY($1::bigint[]) AND bot_id IN "
            "(SELECT bot_id FROM managed_bots WHERE added_by=$2)", list(by), uid)
        for n in names:
            c = by.get(int(n["user_id"]))
            if c:
                c["name"] = c["name"] or (n["first_name"] or "")
                c["username"] = c["username"] or (n["username"] or "")
    return list(by.values())


async def forecast(pool, owner_id: int, *, top: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    customers = await load_customers(pool, owner_id)
    scored = score_all(customers, now)
    buyers = sum(1 for c in customers if c["dates"])
    return {"segments": summarize(scored), "top": scored[:max(0, top)],
            "buyers": buyers, "expected_buyers": round(sum(s["probability"] for s in scored), 1),
            "backtest": backtest(customers, now), "horizon_days": HORIZON_DAYS}


async def tag_segment(pool, owner_id: int, segment: str) -> dict:
    """Поставить тег сегмента на карточки контактов (и снять с выбывших)."""
    if segment not in SEGMENT_TAGS:
        return {"error": "Неизвестный сегмент"}
    tag = SEGMENT_TAGS[segment]
    now = datetime.now(timezone.utc)
    ids = [s["peer_user_id"] for s in score_all(await load_customers(pool, owner_id), now)
           if s["segment"] == segment]
    uid = int(owner_id)
    removed = await pool.execute(
        "UPDATE unified_contacts SET tags = array_remove(tags, $2) "
        "WHERE owner_id=$1 AND $2 = ANY(tags) AND NOT (telegram_user_id = ANY($3::bigint[]))",
        uid, tag, ids)
    added = await pool.execute(
        "UPDATE unified_contacts SET tags = array_append(COALESCE(tags, '{}'), $2) "
        "WHERE owner_id=$1 AND telegram_user_id = ANY($3::bigint[]) "
        "AND NOT ($2 = ANY(COALESCE(tags, '{}')))", uid, tag, ids)
    with_card = await pool.fetchval(
        "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 "
        "AND telegram_user_id = ANY($2::bigint[])", uid, ids)
    return {"tag": tag, "segment": segment, "label": segment_label(segment),
            "in_segment": len(ids), "tagged": int(with_card or 0),
            "without_card": len(ids) - int(with_card or 0),
            "added": _n(added), "removed": _n(removed)}


def _n(status: Optional[str]) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


def setup_routes(app, pool) -> None:
    from services.mini_app_api import _INTERNAL_ERROR, _err, _get_uid, _json_resp, log as _log

    async def get_forecast(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            return _json_resp(await forecast(pool, uid))
        except Exception:
            _log.exception("ai forecast uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def post_tag(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        seg = str((body or {}).get("segment") or "") if isinstance(body, dict) else ""
        try:
            out = await tag_segment(pool, uid, seg)
            if out.get("error"):
                return _err(out["error"], 400)
            return _json_resp(out)
        except Exception:
            _log.exception("ai forecast tag uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/ai/forecast", get_forecast)
    app.router.add_post("/api/miniapp/ai/forecast/tag", post_tag)
