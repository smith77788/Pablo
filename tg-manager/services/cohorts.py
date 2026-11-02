"""Когорты и LTV: кто из пришедших в месяц (неделю) остаётся и сколько приносит.

Зачем. Воронка денег отвечает «какая рассылка принесла деньги сейчас». Но
владельцу нужно и другое: сколько в итоге приносит один привлечённый человек
(LTV), через сколько он покупает, возвращаются ли люди и какой источник даёт
не самых многочисленных, а самых доходных. Это когортный анализ.

Человек попадает в когорту по ПЕРВОМУ касанию с владельцем, из любого канала:

* подписался на бота владельца (bot_users.first_seen);
* получил личную рассылку (dm_campaign_log, только доставленные);
* сам написал на аккаунт флота (inbox_messages) или в Хранилище (vault_messages).

Источник человека — канал этого первого касания («бот @shop», «рассылка
«Осень»»). Кто купил, не оставив ни одного касания (заказ пришёл раньше,
чем мы начали писать журнал), — «Источник неизвестен».

Для каждой когорты по периодам от прихода (0 — месяц/неделя прихода):

* активные — доля когорты, которая в этом периоде написала или купила;
* покупатели — доля, купившая в этом периоде;
* LTV — накопленная выручка на одного человека когорты к концу периода.

Выручка — заказы ботов-продавцов в статусах confirmed/done, пересчитанные в
основную валюту владельца (services/owner_currency.py). Сумма в валюте без
курса в LTV не входит и показывается отдельно — не прячется и не выдумывается.

Чистая часть (`acquisition`, `build`) проверяется тестами без базы; запросы —
тестом против настоящей схемы.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

PAID_ORDER_STATUSES = ("confirmed", "done")
UNKNOWN_SOURCE = "Источник неизвестен"
GRAINS = {"month": 6, "week": 8}         # гранулярность → периодов по умолчанию
MAX_PERIODS = {"month": 12, "week": 26}
_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек")


def _utc(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def period_start(dt: datetime, grain: str) -> date:
    d = _utc(dt).date()
    if grain == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def period_index(start: date, dt: datetime, grain: str) -> int:
    """Номер периода события от начала когорты (0 — период прихода)."""
    p = period_start(dt, grain)
    if grain == "week":
        return (p - start).days // 7
    return (p.year - start.year) * 12 + (p.month - start.month)


def shift(start: date, k: int, grain: str) -> date:
    if grain == "week":
        return start + timedelta(days=7 * k)
    m = start.month - 1 + k
    return date(start.year + m // 12, m % 12 + 1, 1)


def cohort_label(start: date, grain: str) -> str:
    if grain == "week":
        return f"с {start.day:02d}.{start.month:02d}"
    return f"{_MONTHS[start.month - 1]} {start.year}"


# ── Чистая часть ───────────────────────────────────────────────────────────

def acquisition(sources) -> dict:
    """Первое касание каждого человека: {user_id: (at, label)}.

    sources: [(user_id, at, label)] из всех каналов; берётся самое раннее."""
    out: dict = {}
    for uid, at, label in sources:
        if uid is None or at is None:
            continue
        at = _utc(at)
        cur = out.get(uid)
        if cur is None or at < cur[0]:
            out[uid] = (at, label)
    return out


def _round(v, n=2):
    return None if v is None else round(v, n)


def build(acq: dict, orders=(), activity=(), *, grain: str = "month",
          periods: int | None = None, now: datetime | None = None,
          convert=None) -> dict:
    """Собрать когорты и LTV.

    acq:      {user_id: (at, label)} — из `acquisition`;
    orders:   [(user_id, at, amount, currency)] — сумма в единицах валюты;
    activity: [(user_id, at)] — входящие сообщения человека;
    convert:  (amount, currency) → сумма в основной валюте или None (курса нет).
    """
    grain = grain if grain in GRAINS else "month"
    periods = max(2, min(int(periods or GRAINS[grain]), MAX_PERIODS[grain]))
    now = _utc(now or datetime.now(timezone.utc))
    convert = convert or (lambda a, c: float(a))
    first = shift(period_start(now, grain), -(periods - 1), grain)
    cur_idx_of = {}                       # начало когорты → номер текущего периода

    acq = dict(acq)
    # Покупатель без единого касания — тоже человек, его деньги не теряем.
    for uid, at, _a, _c in sorted(orders, key=lambda o: _utc(o[1])):
        if uid not in acq:
            acq[uid] = (_utc(at), UNKNOWN_SOURCE)

    members: dict = {}                    # user_id → начало когорты
    for uid, (at, _label) in acq.items():
        st = period_start(at, grain)
        if st >= first:
            members[uid] = st
            cur_idx_of.setdefault(st, period_index(st, now, grain))

    size: dict = {}
    for st in members.values():
        size[st] = size.get(st, 0) + 1
    active: dict = {}                     # (cohort, k) → {user}
    buyers: dict = {}
    revenue: dict = {}                    # (cohort, k) → сумма
    per_user_rev: dict = {}
    per_user_orders: dict = {}
    first_buy: dict = {}
    unconverted: dict = {}

    def _k(uid, at):
        st = members.get(uid)
        if st is None:
            return None, None
        k = max(0, period_index(st, at, grain))
        return st, (k if k < periods else None)

    for uid, at in activity:
        st, k = _k(uid, at)
        if k is not None:
            active.setdefault((st, k), set()).add(uid)
    for uid, at, amount, cur in orders:
        st, k = _k(uid, at)
        if st is None:
            continue
        if k is not None:
            active.setdefault((st, k), set()).add(uid)
            buyers.setdefault((st, k), set()).add(uid)
        per_user_orders[uid] = per_user_orders.get(uid, 0) + 1
        at = _utc(at)
        if uid not in first_buy or at < first_buy[uid]:
            first_buy[uid] = at
        v = convert(amount, cur)
        if v is None:
            c = str(cur or "?").upper()
            unconverted[c] = unconverted.get(c, 0.0) + float(amount or 0)
            continue
        per_user_rev[uid] = per_user_rev.get(uid, 0.0) + v
        if k is not None:
            revenue[(st, k)] = revenue.get((st, k), 0.0) + v

    cohorts = []
    for st in sorted(size, reverse=True):
        n = size[st]
        last = min(cur_idx_of[st], periods - 1)
        act, buy, ltv = [], [], []
        acc = 0.0
        for k in range(periods):
            if k > last:
                act.append(None)
                buy.append(None)
                ltv.append(None)
                continue
            acc += revenue.get((st, k), 0.0)
            act.append(round(len(active.get((st, k), ())) / n, 4))
            buy.append(round(len(buyers.get((st, k), ())) / n, 4))
            ltv.append(round(acc / n, 2))
        cohorts.append({"start": st.isoformat(), "label": cohort_label(st, grain),
                        "size": n, "active": act, "buyers": buy, "ltv": ltv})

    # Средняя кривая LTV: по каждому периоду — только когорты, дожившие до него,
    # взвешенно по размеру. Иначе молодые когорты тянули бы хвост вниз.
    curve = []
    for k in range(periods):
        num_ = den = 0.0
        for c in cohorts:
            if c["ltv"][k] is not None:
                num_ += c["ltv"][k] * c["size"]
                den += c["size"]
        curve.append(round(num_ / den, 2) if den else None)

    by_src: dict = {}
    for uid in members:
        label = acq[uid][1]
        s = by_src.setdefault(label, {"label": label, "people": 0, "buyers": 0,
                                      "revenue": 0.0, "orders": 0, "_days": []})
        s["people"] += 1
        if uid in per_user_orders:
            s["buyers"] += 1
            s["orders"] += per_user_orders[uid]
            s["_days"].append((first_buy[uid] - acq[uid][0]).total_seconds() / 86400)
        s["revenue"] += per_user_rev.get(uid, 0.0)
    sources = []
    for s in by_src.values():
        days = sorted(s.pop("_days"))
        s["revenue"] = round(s["revenue"], 2)
        s["ltv"] = round(s["revenue"] / s["people"], 2) if s["people"] else None
        s["paying_share"] = round(s["buyers"] / s["people"], 4) if s["people"] else None
        s["arppu"] = round(s["revenue"] / s["buyers"], 2) if s["buyers"] else None
        s["median_days_to_buy"] = (round(days[len(days) // 2], 1) if days else None)
        sources.append(s)
    sources.sort(key=lambda s: (s["label"] == UNKNOWN_SOURCE, -s["revenue"], -s["people"]))

    people = len(members)
    payers = sum(1 for u in members if u in per_user_orders)
    total_rev = round(sum(per_user_rev.get(u, 0.0) for u in members), 2)
    return {
        "grain": grain,
        "periods": periods,
        "cohorts": cohorts,
        "ltv_curve": curve,
        "sources": sources,
        "totals": {
            "people": people,
            "buyers": payers,
            "orders": sum(per_user_orders.get(u, 0) for u in members),
            "revenue": total_rev,
            "ltv": round(total_rev / people, 2) if people else None,
            "arppu": round(total_rev / payers, 2) if payers else None,
            "paying_share": round(payers / people, 4) if people else None,
        },
        "unconverted": [{"currency": c, "amount": round(v, 2)}
                        for c, v in sorted(unconverted.items())],
    }


# ── Выборка из базы ────────────────────────────────────────────────────────

async def _safe(pool, q, *a):
    try:
        return list(await pool.fetch(q, *a) or [])
    except Exception:
        log.debug("cohorts: источник недоступен: %s", q[:60], exc_info=True)
        return []


async def gather(pool, owner_id: int, since: datetime) -> tuple[list, list, list]:
    """Касания (за всё время — нужен именно ПЕРВЫЙ), заказы и активность с `since`."""
    uid = int(owner_id)
    sources = []
    for r in await _safe(pool,
            """SELECT bu.user_id, MIN(bu.first_seen) AS at, b.username
                 FROM bot_users bu JOIN managed_bots b ON b.bot_id = bu.bot_id
                WHERE b.added_by=$1 AND bu.first_seen IS NOT NULL
                GROUP BY bu.user_id, b.bot_id, b.username""", uid):
        sources.append((r["user_id"], r["at"],
                        f"бот @{r['username']}" if r["username"] else "бот"))
    for r in await _safe(pool,
            """SELECT l.tg_user_id AS user_id, MIN(l.sent_at) AS at, c.name
                 FROM dm_campaign_log l JOIN dm_campaigns c ON c.id = l.campaign_id
                WHERE c.owner_id=$1 AND l.status='sent' AND l.sent_at IS NOT NULL
                GROUP BY l.tg_user_id, c.id, c.name""", uid):
        sources.append((r["user_id"], r["at"], f"рассылка «{r['name'] or 'без названия'}»"))
    for r in await _safe(pool,
            """SELECT peer_user_id AS user_id, MIN(msg_date) AS at FROM inbox_messages
                WHERE owner_id=$1 GROUP BY peer_user_id""", uid):
        sources.append((r["user_id"], r["at"], "написал на аккаунт"))
    for r in await _safe(pool,
            """SELECT chat_id AS user_id, MIN(COALESCE(msg_date, created_at)) AS at
                 FROM vault_messages
                WHERE owner_id=$1 AND direction='in' AND chat_id > 0
                GROUP BY chat_id""", uid):
        sources.append((r["user_id"], r["at"], "написал в бизнес-аккаунт"))

    orders = [(r["user_id"], r["at"], (r["total_cents"] or 0) / 100, r["currency"])
              for r in await _safe(pool,
                  """SELECT customer_chat_id AS user_id, created_at AS at, total_cents, currency
                       FROM bot_sales_orders
                      WHERE owner_id=$1 AND status = ANY($2::text[]) AND created_at >= $3""",
                  uid, list(PAID_ORDER_STATUSES), since)]

    activity = []
    for q in (
        """SELECT peer_user_id AS user_id, MIN(msg_date) AS at FROM inbox_messages
            WHERE owner_id=$1 AND msg_date >= $2
            GROUP BY peer_user_id, date_trunc('day', msg_date)""",
        """SELECT chat_id AS user_id, MIN(COALESCE(msg_date, created_at)) AS at
             FROM vault_messages
            WHERE owner_id=$1 AND direction='in' AND chat_id > 0
              AND COALESCE(msg_date, created_at) >= $2
            GROUP BY chat_id, date_trunc('day', COALESCE(msg_date, created_at))""",
        """SELECT s.user_id, MIN(m.created_at) AS at FROM relay_messages m
             JOIN relay_sessions s ON s.id = m.session_id
             JOIN managed_bots b ON b.bot_id = s.bot_id
            WHERE b.added_by=$1 AND m.direction='in' AND m.created_at >= $2
            GROUP BY s.user_id, date_trunc('day', m.created_at)""",
        """SELECT customer_chat_id AS user_id, last_at AS at FROM bot_sales_dialogs
            WHERE owner_id=$1 AND last_at >= $2 AND msg_count > 0""",
    ):
        activity += [(r["user_id"], r["at"]) for r in await _safe(pool, q, uid, since)]
    return sources, orders, activity


async def report(pool, owner_id: int, grain: str = "month", periods: int | None = None,
                 fetch_rates=None) -> dict:
    """Когорты и LTV владельца в его основной валюте."""
    from services import owner_currency

    grain = grain if grain in GRAINS else "month"
    periods = max(2, min(int(periods or GRAINS[grain]), MAX_PERIODS[grain]))
    now = datetime.now(timezone.utc)
    first = shift(period_start(now, grain), -(periods - 1), grain)
    since = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    conv, money = await owner_currency.load(pool, owner_id, fetch=fetch_rates)
    sources, orders, activity = await gather(pool, owner_id, since)
    out = build(acquisition(sources), orders, activity, grain=grain, periods=periods,
                now=now, convert=conv.convert)
    out["money"] = money
    return out
