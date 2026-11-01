"""Воронка денег: охват → подписка → ответ → покупка, с атрибуцией по кампании.

Зачем. Аналитика продукта мерила систему (сколько отправлено, сколько
аккаунтов живо), а владельцу нужен ответ «какая рассылка принесла деньги и
сколько стоил лид». Данные для этого уже лежат в базе, но в разных таблицах и
ни разу не сведены:

* охват — dm_campaign_log (кому кампания реально доставила сообщение);
* подписка — bot_users (человек запустил бота владельца);
* ответ — inbox_messages (ответ на аккаунт флота), vault_messages (Хранилище),
  relay_messages (релей бота), bot_sales_dialogs (разговор с ботом-продавцом);
* покупка — bot_sales_orders в статусах confirmed/done;
* расход — campaign_spend (вводит владелец: цену аккаунтов и прокси система
  не знает).

Атрибуция — ПОСЛЕДНЕЕ касание в окне: событие человека засчитывается той
кампании, которая написала ему последней не раньше чем за ATTRIBUTION_WINDOW_DAYS
до события. Событие без такого касания — «Без рассылки» (органика, реклама,
сарафан). Каждый шаг считается по людям, а не по сообщениям: десять ответов
одного человека — один ответивший.

Лид = человек, который ответил. Стоимость лида = расход / ответившие.

Чистая часть (`attribute`, `summarize`) проверяется тестами без базы; запросы —
тестом против настоящей схемы.
"""
from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ATTRIBUTION_WINDOW_DAYS = 30
PAID_ORDER_STATUSES = ("confirmed", "done")
NO_CAMPAIGN = 0           # ключ корзины «Без рассылки»
STEPS = ("reached", "subscribed", "replied", "bought")
STEP_LABEL = {
    "reached": "Получили сообщение",
    "subscribed": "Подписались на бота",
    "replied": "Ответили",
    "bought": "Купили",
}
_Z95 = 1.96


def _utc(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


# ── Чистая часть ───────────────────────────────────────────────────────────

def attribute(touches, subscriptions=(), replies=(), orders=(),
              window_days: int = ATTRIBUTION_WINDOW_DAYS) -> dict:
    """Разложить события по кампаниям (последнее касание в окне).

    touches:       [(campaign_id, user_id, at)] — первая доставка кампании человеку;
    subscriptions: [(user_id, at)];
    replies:       [(user_id, at)];
    orders:        [(user_id, at, total_cents, currency)].

    Возвращает {campaign_id: {"reached": set, "subscribed": set, "replied": set,
    "bought": set, "revenue": {currency: cents}, "orders": int}}; корзина
    NO_CAMPAIGN — события без касания в окне.
    """
    window = timedelta(days=int(window_days))
    by_user: dict[int, list] = {}
    out: dict[int, dict] = {}

    def _bucket(cid):
        b = out.get(cid)
        if b is None:
            b = {"reached": set(), "subscribed": set(), "replied": set(),
                 "bought": set(), "revenue": {}, "orders": 0}
            out[cid] = b
        return b

    for cid, uid, at in touches or ():
        if uid is None or at is None:
            continue
        by_user.setdefault(int(uid), []).append((_utc(at), int(cid)))
        _bucket(int(cid))["reached"].add(int(uid))
    for lst in by_user.values():
        lst.sort()
    keys = {u: [t for t, _ in lst] for u, lst in by_user.items()}

    def _owner_campaign(uid, at):
        lst = by_user.get(uid)
        if not lst or at is None:
            return NO_CAMPAIGN
        at = _utc(at)
        i = bisect.bisect_right(keys[uid], at) - 1
        if i < 0:
            return NO_CAMPAIGN
        t, cid = lst[i]
        return cid if at - t <= window else NO_CAMPAIGN

    for uid, at in subscriptions or ():
        if uid is not None:
            _bucket(_owner_campaign(int(uid), at))["subscribed"].add(int(uid))
    for uid, at in replies or ():
        if uid is not None:
            _bucket(_owner_campaign(int(uid), at))["replied"].add(int(uid))
    for uid, at, cents, cur in orders or ():
        if uid is None:
            continue
        b = _bucket(_owner_campaign(int(uid), at))
        b["bought"].add(int(uid))
        b["orders"] += 1
        cur = cur or "USD"
        b["revenue"][cur] = b["revenue"].get(cur, 0) + int(cents or 0)
    return out


def _z(n_a, c_a, n_b, c_b) -> float:
    from services.ab_engine import _two_prop_z
    return _two_prop_z(n_a, c_a, n_b, c_b)


def _rate(num, den):
    return round(num / den, 4) if den else None


def summarize(buckets: dict, names: dict | None = None, spend: dict | None = None,
              min_sample: int = 30) -> dict:
    """Свести корзины атрибуции в ответ экрана.

    names: {campaign_id: имя}; spend: {campaign_id: {"amount": float, "currency": str}}.

    Для каждой кампании: шаги воронки (люди), доли от охвата, выручка по
    валютам, расход, стоимость лида (расход / ответившие), стоимость покупателя,
    окупаемость (если расход и выручка в одной валюте). Плюс сравнение
    ответов с лучшей кампанией: z-тест двух долей, значимо при |z| ≥ 1,96 и
    выборке не меньше min_sample у обеих.
    """
    names = names or {}
    spend = spend or {}
    rows = []
    totals = {s: set() for s in STEPS}
    total_rev: dict[str, int] = {}
    total_orders = 0
    for cid, b in buckets.items():
        for s in STEPS:
            totals[s] |= b[s]
        for cur, v in b["revenue"].items():
            total_rev[cur] = total_rev.get(cur, 0) + v
        total_orders += b["orders"]
        n = {s: len(b[s]) for s in STEPS}
        sp = spend.get(cid) or {}
        amount = float(sp.get("amount") or 0)
        sp_cur = sp.get("currency") or None
        rev_same = b["revenue"].get(sp_cur) if sp_cur else None
        rows.append({
            "campaign_id": cid,
            "name": ("Без рассылки" if cid == NO_CAMPAIGN
                     else names.get(cid) or f"Кампания #{cid}"),
            "steps": n,
            "rates": {s: _rate(n[s], n["reached"]) for s in STEPS[1:]},
            "orders": b["orders"],
            "revenue": [{"currency": c, "total_cents": v}
                        for c, v in sorted(b["revenue"].items())],
            "spend": ({"amount": amount, "currency": sp_cur} if amount else None),
            "cost_per_lead": (round(amount / n["replied"], 2)
                              if amount and n["replied"] else None),
            "cost_per_buyer": (round(amount / n["bought"], 2)
                               if amount and n["bought"] else None),
            "romi": (round((rev_same / 100 - amount) / amount, 3)
                     if amount and rev_same is not None else None),
        })
    # Сравнение по ответам — только среди настоящих кампаний: у органики
    # охвата нет, доля ответа у неё не определена.
    real = [r for r in rows if r["campaign_id"] != NO_CAMPAIGN and r["steps"]["reached"]]
    best = None
    if len(real) >= 2:
        best = max(real, key=lambda r: (r["rates"]["replied"] or 0, r["steps"]["reached"]))
        for r in real:
            if r is best:
                r["vs_best"] = {"best": True}
                continue
            na, ca = best["steps"]["reached"], best["steps"]["replied"]
            nb, cb = r["steps"]["reached"], r["steps"]["replied"]
            z = _z(na, ca, nb, cb)
            r["vs_best"] = {
                "best": False, "z": round(z, 2),
                "significant": bool(na >= min_sample and nb >= min_sample
                                    and abs(z) >= _Z95),
                "enough_data": bool(na >= min_sample and nb >= min_sample),
            }
    rows.sort(key=lambda r: (r["campaign_id"] == NO_CAMPAIGN,
                             -sum(r_["total_cents"] for r_ in r["revenue"]),
                             -r["steps"]["replied"], -r["steps"]["reached"]))
    n_tot = {s: len(totals[s]) for s in STEPS}
    total_spend: dict[str, float] = {}
    for sp in spend.values():
        if sp and float(sp.get("amount") or 0):
            c = sp.get("currency") or "RUB"
            total_spend[c] = total_spend.get(c, 0.0) + float(sp["amount"])
    one_cur = list(total_spend.items())[0] if len(total_spend) == 1 else None
    return {
        "steps": [{"key": s, "label": STEP_LABEL[s], "count": n_tot[s],
                   "rate": _rate(n_tot[s], n_tot["reached"]) if s != "reached" else None}
                  for s in STEPS],
        "orders": total_orders,
        "revenue": [{"currency": c, "total_cents": v} for c, v in sorted(total_rev.items())],
        "spend": [{"currency": c, "amount": round(v, 2)} for c, v in sorted(total_spend.items())],
        "cost_per_lead": (round(one_cur[1] / n_tot["replied"], 2)
                          if one_cur and n_tot["replied"] else None),
        "campaigns": rows,
        "best_campaign_id": best["campaign_id"] if best else None,
        "window_days": ATTRIBUTION_WINDOW_DAYS,
    }


# ── Выборка из базы ────────────────────────────────────────────────────────

async def _safe(pool, q, *a):
    try:
        return list(await pool.fetch(q, *a) or [])
    except Exception:
        log.debug("money_funnel: источник недоступен: %s", q[:60], exc_info=True)
        return []


async def build(pool, owner_id: int, days: int = 30) -> dict:
    """Воронка денег владельца за последние `days` дней."""
    uid = int(owner_id)
    days = max(1, min(int(days or 30), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    touch_since = since - timedelta(days=ATTRIBUTION_WINDOW_DAYS)

    t_rows = await _safe(pool,
        """SELECT l.campaign_id, l.tg_user_id, MIN(l.sent_at) AS at
             FROM dm_campaign_log l
             JOIN dm_campaigns c ON c.id = l.campaign_id
            WHERE c.owner_id=$1 AND l.status='sent' AND l.sent_at >= $2
            GROUP BY l.campaign_id, l.tg_user_id""", uid, touch_since)
    # Охват считаем за период, а касания до него нужны только для атрибуции
    # событий периода: кампания позапрошлого месяца не должна раздувать охват.
    touches = [(r["campaign_id"], r["tg_user_id"], r["at"]) for r in t_rows]
    in_period = {(r["campaign_id"], r["tg_user_id"]) for r in t_rows
                 if _utc(r["at"]) >= since}

    subs = await _safe(pool,
        """SELECT bu.user_id, bu.first_seen AS at FROM bot_users bu
             JOIN managed_bots b ON b.bot_id = bu.bot_id
            WHERE b.added_by=$1 AND bu.first_seen >= $2""", uid, since)
    # Ответы — по человеку И дню: если брать только самый ранний, человек,
    # писавший до кампании и ответивший после неё, достался бы «Без рассылки».
    replies = []
    replies += await _safe(pool,
        """SELECT peer_user_id AS user_id, MIN(msg_date) AS at FROM inbox_messages
            WHERE owner_id=$1 AND msg_date >= $2 GROUP BY peer_user_id, date_trunc('day', msg_date)""", uid, since)
    replies += await _safe(pool,
        """SELECT chat_id AS user_id, MIN(COALESCE(msg_date, created_at)) AS at
             FROM vault_messages
            WHERE owner_id=$1 AND direction='in' AND chat_id > 0
              AND COALESCE(msg_date, created_at) >= $2
            GROUP BY chat_id, date_trunc('day', COALESCE(msg_date, created_at))""", uid, since)
    replies += await _safe(pool,
        """SELECT s.user_id, MIN(m.created_at) AS at FROM relay_messages m
             JOIN relay_sessions s ON s.id = m.session_id
             JOIN managed_bots b ON b.bot_id = s.bot_id
            WHERE b.added_by=$1 AND m.direction='in' AND m.created_at >= $2
            GROUP BY s.user_id, date_trunc('day', m.created_at)""", uid, since)
    replies += await _safe(pool,
        """SELECT customer_chat_id AS user_id, MIN(created_at) AS at
             FROM bot_sales_dialogs
            WHERE owner_id=$1 AND created_at >= $2 AND msg_count > 0
            GROUP BY customer_chat_id, date_trunc('day', created_at)""", uid, since)
    orders = await _safe(pool,
        """SELECT customer_chat_id AS user_id, created_at AS at, total_cents, currency
             FROM bot_sales_orders
            WHERE owner_id=$1 AND status = ANY($2::text[]) AND created_at >= $3""",
        uid, list(PAID_ORDER_STATUSES), since)

    buckets = attribute(
        touches,
        [(r["user_id"], r["at"]) for r in subs],
        [(r["user_id"], r["at"]) for r in replies],
        [(r["user_id"], r["at"], r["total_cents"], r["currency"]) for r in orders])
    for cid, b in buckets.items():
        if cid != NO_CAMPAIGN:
            b["reached"] = {u for u in b["reached"] if (cid, u) in in_period}
    # Кампания, у которой в периоде нет ни охвата, ни событий, — шум.
    buckets = {c: b for c, b in buckets.items()
               if any(b[s] for s in STEPS) or b["orders"]}

    names, spend = {}, {}
    ids = [c for c in buckets if c != NO_CAMPAIGN]
    if ids:
        for r in await _safe(pool,
                "SELECT id, name FROM dm_campaigns WHERE owner_id=$1 AND id = ANY($2::bigint[])",
                uid, ids):
            names[r["id"]] = r["name"]
        for r in await _safe(pool,
                """SELECT campaign_id, amount, currency FROM campaign_spend
                    WHERE owner_id=$1 AND campaign_kind='dm'
                      AND campaign_id = ANY($2::bigint[])""", uid, ids):
            spend[r["campaign_id"]] = {"amount": float(r["amount"] or 0),
                                       "currency": r["currency"]}
    out = summarize(buckets, names, spend)
    out["days"] = days
    return out


async def set_spend(pool, owner_id: int, campaign_id: int, amount, currency: str = "RUB") -> dict:
    """Записать расход на кампанию. Кампания обязана быть своей."""
    uid, cid = int(owner_id), int(campaign_id)
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("Сумма должна быть числом") from None
    if amount < 0 or amount > 1e11:
        raise ValueError("Сумма вне допустимых пределов")
    currency = (currency or "RUB").strip().upper()[:8] or "RUB"
    own = await pool.fetchval(
        "SELECT 1 FROM dm_campaigns WHERE id=$1 AND owner_id=$2", cid, uid)
    if not own:
        raise LookupError("Кампания не найдена")
    await pool.execute(
        """INSERT INTO campaign_spend (owner_id, campaign_kind, campaign_id, amount, currency, updated_at)
           VALUES ($1, 'dm', $2, $3, $4, now())
           ON CONFLICT (owner_id, campaign_kind, campaign_id)
           DO UPDATE SET amount=EXCLUDED.amount, currency=EXCLUDED.currency, updated_at=now()""",
        uid, cid, amount, currency)
    return {"campaign_id": cid, "amount": amount, "currency": currency}
