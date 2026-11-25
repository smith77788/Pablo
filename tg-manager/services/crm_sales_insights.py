"""Аналитика продаж поверх воронки: динамика, где теряем, какой аккаунт флота
приносит сделки и какие сделки брошены без следующего шага.

ЗАЧЕМ. Аналитика воронки (services/crm_funnel_analytics) отвечает на вопросы о
ДВИЖЕНИИ по стадиям: конверсия, время в стадии, застрявшие, прогноз. Четыре
вопроса продавца она оставляла без ответа:

* «стало лучше или хуже, чем в прошлом периоде?» — одно окно без сравнения
  не говорит, растёт ли бизнес. Здесь — то же окно против предыдущего такой
  же длины и помесячная динамика за полгода;
* «на какой стадии я теряю сделки?» — доля выигранных знает, СКОЛЬКО
  проиграно, но не ГДЕ. История переходов (crm_stage_history) помнит, из
  какой стадии сделка ушла в «Проиграно»;
* «какой из моих аккаунтов приносит деньги?» — продукт ведёт флот
  аккаунтов, и контакт знает аккаунт, который его нашёл первым
  (contact_sources.first_seen_at). Атрибуция по первому касанию — стандарт
  отчётов об источниках у CRM-лидеров; здесь источником служит аккаунт флота;
* «какие сделки я бросил?» — «застрявшие» меряют время в СТАДИИ, а сделка
  может двигаться по стадиям и при этом месяц лежать без касания и без
  напоминания. У Pipedrive это «сделки без следующего действия» — первая
  причина тихо потерянных денег. Последнее касание — самое позднее из
  последнего взаимодействия (contact_crm.last_interaction_at), записи в
  журнале касаний, настоящей смены стадии и правки простой сделки.

ЧЕСТНОСТЬ. Суммы — только по валютам (рубли с долларами не складываются, см.
crm_engine.pipeline). Доля выигранных по источнику идёт с 95% интервалом
Уилсона тем же помощником, что у воронки: «1 из 1» не обязан выглядеть лучше
«40 из 60». Строки истории, восстановленные при накате (backfilled), времени
перехода не знают: для «где теряем» стадия неизвестна, если проигрыш известен
только по такой строке, и ответ называет это «стадия неизвестна», а не
приписывает его «Лиду».

Чистая часть (`analyze`) проверяется без базы; запросы — против настоящей
схемы (tests/test_crm_sales_insights_postgres.py).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from services.crm_funnel_analytics import (
    DEFAULT_WINDOW,
    OPEN_STAGES,
    WINDOWS,
    _days,
    _deal_paths,
    _round,
    _utc,
    wilson,
)

log = logging.getLogger(__name__)

WON, LOST = "won", "lost"  # те же ключи, что crm_engine.STAGE_LABELS

NEGLECT_DAYS = 14        # без касания дольше — сделка брошена
NEGLECTED_LIMIT = 20
SOURCES_LIMIT = 10
MONTHS = 6
UNKNOWN_STAGE = "unknown"
NO_SOURCE = "none"


def _add(bucket: dict, cur: str, val: float) -> None:
    if val:
        bucket[cur] = round(bucket.get(cur, 0.0) + val, 2)


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _last_months(now: datetime, n: int) -> list[str]:
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _period() -> dict:
    return {"new": 0, "won": 0, "lost": 0, "won_by_currency": {}}


def _delta(cur, prev):
    """Изменение к прошлому периоду; None — сравнивать не с чем (было 0)."""
    if cur is None or prev is None:
        return None
    if prev == 0:
        return None
    return round((cur - prev) / prev, 3)


def analyze(deals: list[dict], transitions: list[dict], meta: dict, *,
            now: datetime, window_days: int = DEFAULT_WINDOW,
            main_currency: str = "RUB", neglect_days: float = NEGLECT_DAYS) -> dict:
    """Весь отчёт.

    deals и transitions — те же, что у crm_funnel_analytics.gather. meta —
    по ключу (deal_kind, deal_key): source (id аккаунта-источника или None),
    next_reminder_at, last_touch_at (самое позднее касание вне истории
    стадий: взаимодействие, журнал касаний, правка сделки).
    """
    now = _utc(now)
    since = now - timedelta(days=window_days)
    prev_since = since - timedelta(days=window_days)
    paths = _deal_paths(deals, transitions)
    months = _last_months(now, MONTHS)
    monthly = {k: _period() for k in months}
    cur_p, prev_p = _period(), _period()
    losses: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    neglected = []
    open_n = 0

    def _in(p, field, cur=None, val=0.0):
        p[field] += 1
        if cur is not None:
            _add(p["won_by_currency"], cur, val)

    for d in deals:
        key = (d["deal_kind"], str(d["deal_key"]))
        path = paths.get(key, [])
        m = meta.get(key, {})
        stage = d.get("stage") or "lead"
        cur = (d.get("currency") or main_currency or "RUB").upper()
        val = float(d.get("value") or 0)
        first_at = _utc(path[0]["changed_at"]) if path else _utc(d.get("created_at"))
        closing = next((t for t in reversed(path) if t["to_stage"] in (WON, LOST)), None)
        closed_at = _utc(closing["changed_at"]) if closing and stage in (WON, LOST) else None

        # ── периоды: новые по первому появлению, закрытые по дате закрытия
        if first_at is not None:
            if first_at >= since:
                cur_p["new"] += 1
            elif first_at >= prev_since:
                prev_p["new"] += 1
            mk = _month_key(first_at)
            if mk in monthly:
                monthly[mk]["new"] += 1
        if closed_at is not None:
            field = "won" if stage == WON else "lost"
            won_args = (cur, val) if stage == WON else (None, 0.0)
            if closed_at >= since:
                _in(cur_p, field, *won_args)
            elif closed_at >= prev_since:
                _in(prev_p, field, *won_args)
            mk = _month_key(closed_at)
            if mk in monthly:
                _in(monthly[mk], field, *won_args)

        # ── где теряем: стадия, из которой ушла в «Проиграно»
        if stage == LOST and closed_at is not None and closed_at >= since:
            frm = closing.get("from_stage") if closing else None
            if closing is None or closing.get("backfilled") or frm not in OPEN_STAGES:
                frm = UNKNOWN_STAGE
            row = losses.setdefault(frm, {"stage": frm, "count": 0, "value_by_currency": {}})
            row["count"] += 1
            _add(row["value_by_currency"], cur, val)

        # ── источники: только сделки, появившиеся в окне (как когорта воронки)
        if first_at is not None and first_at >= since:
            src = m.get("source")
            sk = str(src) if src is not None else NO_SOURCE
            s = sources.setdefault(sk, {"source": sk, "leads": 0, "won": 0, "lost": 0,
                                        "open": 0, "won_by_currency": {}})
            s["leads"] += 1
            if stage == WON:
                s["won"] += 1
                _add(s["won_by_currency"], cur, val)
            elif stage == LOST:
                s["lost"] += 1
            else:
                s["open"] += 1

        # ── брошенные: открыта, нет будущего напоминания, давно без касания
        if stage in OPEN_STAGES:
            open_n += 1
            rem = _utc(m.get("next_reminder_at"))
            if rem is not None and rem >= now:
                continue
            touches = [_utc(m.get("last_touch_at"))] + [
                _utc(t["changed_at"]) for t in path if not t.get("backfilled")]
            touches = [t for t in touches if t is not None]
            last = max(touches) if touches else _utc(d.get("created_at"))
            if last is None:
                continue
            idle = _days(now - last)
            if idle > neglect_days:
                neglected.append({
                    "deal_kind": d["deal_kind"], "deal_key": str(d["deal_key"]),
                    "name": d.get("name") or "", "stage": stage,
                    "idle_days": _round(idle), "value": val, "currency": cur,
                    "reminder_overdue": rem is not None,
                    "touch_known": bool(touches),
                })

    for p in (cur_p, prev_p):
        n = p["won"] + p["lost"]
        p["win_rate"] = _round(p["won"] / n, 3) if n else None
    compare = {
        "new": _delta(cur_p["new"], prev_p["new"]),
        "won": _delta(cur_p["won"], prev_p["won"]),
        "win_rate_pp": (round(cur_p["win_rate"] - prev_p["win_rate"], 3)
                        if cur_p["win_rate"] is not None and prev_p["win_rate"] is not None
                        else None),
        "won_by_currency": {
            c: _delta(cur_p["won_by_currency"].get(c, 0.0), prev_p["won_by_currency"].get(c))
            for c in set(cur_p["won_by_currency"]) | set(prev_p["won_by_currency"])
        },
    }

    lost_total = sum(r["count"] for r in losses.values())
    order = {s: i for i, s in enumerate(OPEN_STAGES + (UNKNOWN_STAGE,))}
    loss_rows = sorted(losses.values(), key=lambda r: order.get(r["stage"], 99))
    for r in loss_rows:
        r["share"] = round(r["count"] / lost_total, 3) if lost_total else None
    worst = max((r for r in loss_rows if r["stage"] != UNKNOWN_STAGE),
                key=lambda r: r["count"], default=None)

    src_rows = []
    for s in sources.values():
        closed = s["won"] + s["lost"]
        s["win_rate"] = _round(s["won"] / closed, 3) if closed else None
        ci = wilson(s["won"], closed)
        s["win_rate_ci"] = [round(x, 3) for x in ci] if ci else None
        src_rows.append(s)
    # Сначала то, что приносит выигрыши, затем объём лидов; «без источника» —
    # честная строка, но в конце, чтобы не заслоняла аккаунты.
    src_rows.sort(key=lambda s: (s["source"] == NO_SOURCE, -s["won"], -s["leads"], s["source"]))

    neglected.sort(key=lambda x: (-x["value"], -(x["idle_days"] or 0)))

    return {
        "window_days": window_days,
        "neglect_days": neglect_days,
        "period": cur_p,
        "previous": prev_p,
        "compare": compare,
        "monthly": [{"month": k, **monthly[k]} for k in months],
        "losses": loss_rows,
        "losses_total": lost_total,
        "worst_loss_stage": worst["stage"] if worst and worst["count"] else None,
        "sources": src_rows[:SOURCES_LIMIT],
        "sources_total": len(src_rows),
        "neglected": neglected[:NEGLECTED_LIMIT],
        "neglected_total": len(neglected),
        "open": open_n,
    }


# ── Запросы ─────────────────────────────────────────────────────────────────

async def gather_meta(pool, owner_id: int) -> dict:
    """Источник, напоминание и последнее касание по каждой сделке владельца.

    Источник контакта — аккаунт, который нашёл его ПЕРВЫМ, и только живой
    аккаунт этого же владельца: contact_sources своего owner_id не имеет
    (schema_v138), поэтому скоуп держит join на tg_accounts.owner_id.
    """
    contacts = await pool.fetch(
        """SELECT cc.contact_id::text AS deal_key, cc.next_reminder_at,
                  GREATEST(cc.last_interaction_at, act.last_at) AS last_touch_at,
                  src.account_id AS source
             FROM contact_crm cc
             LEFT JOIN LATERAL (
                  SELECT MAX(a.created_at) AS last_at FROM contact_crm_activity a
                   WHERE a.owner_id = cc.owner_id AND a.contact_id = cc.contact_id) act ON TRUE
             LEFT JOIN LATERAL (
                  SELECT cs.account_id FROM contact_sources cs
                    JOIN tg_accounts ta ON ta.id = cs.account_id AND ta.owner_id = cc.owner_id
                   WHERE cs.contact_id = cc.contact_id
                   ORDER BY cs.first_seen_at NULLS LAST, cs.id
                   LIMIT 1) src ON TRUE
            WHERE cc.owner_id = $1""",
        owner_id)
    deals = await pool.fetch(
        """SELECT d.id::text AS deal_key,
                  GREATEST(d.updated_at, act.last_at) AS last_touch_at
             FROM crm_deals d
             LEFT JOIN LATERAL (
                  SELECT MAX(a.created_at) AS last_at FROM crm_activity a
                   WHERE a.owner_id = d.owner_id AND a.deal_id = d.id) act ON TRUE
            WHERE d.owner_id = $1""",
        owner_id)
    meta: dict = {}
    for r in contacts:
        meta[("contact", r["deal_key"])] = {
            "source": r["source"], "next_reminder_at": r["next_reminder_at"],
            "last_touch_at": r["last_touch_at"]}
    for r in deals:
        meta[("deal", r["deal_key"])] = {
            "source": None, "next_reminder_at": None,
            "last_touch_at": r["last_touch_at"]}
    return meta


def account_label(row) -> str:
    """Подпись аккаунта без полного телефона: имя, @ник, иначе хвост номера."""
    name = (row.get("first_name") or "").strip()
    if name:
        return name
    uname = (row.get("username") or "").strip()
    if uname:
        return "@" + uname
    phone = "".join(ch for ch in str(row.get("phone") or "") if ch.isdigit())
    return f"Аккаунт ···{phone[-4:]}" if len(phone) >= 4 else f"Аккаунт #{row.get('id')}"


async def report(pool, owner_id: int, window_days: int = DEFAULT_WINDOW) -> dict:
    from services import crm_funnel_analytics as fa
    from services.contacts_hub.crm_engine import STAGE_LABELS
    from services.mini_app_deals_board import owner_main_currency

    if window_days not in WINDOWS:
        window_days = DEFAULT_WINDOW
    main = await owner_main_currency(pool, owner_id)
    deals, hist = await fa.gather(pool, owner_id, main)
    meta = await gather_meta(pool, owner_id)
    out = await asyncio.to_thread(
        analyze, deals, hist, meta, now=datetime.now(timezone.utc),
        window_days=window_days, main_currency=main)

    ids = [int(s["source"]) for s in out["sources"] if s["source"] != NO_SOURCE]
    labels = {}
    if ids:
        rows = await pool.fetch(
            "SELECT id, first_name, username, phone FROM tg_accounts "
            "WHERE owner_id = $1 AND id = ANY($2::int[])", owner_id, ids)
        labels = {str(r["id"]): account_label(dict(r)) for r in rows}
    for s in out["sources"]:
        s["label"] = (labels.get(s["source"]) or "Аккаунт удалён") \
            if s["source"] != NO_SOURCE else "Без аккаунта-источника"
    for r in out["losses"]:
        r["label"] = STAGE_LABELS.get(r["stage"], "Стадия неизвестна")
    if out["worst_loss_stage"]:
        out["worst_loss_label"] = STAGE_LABELS.get(out["worst_loss_stage"], "")
    for r in out["neglected"]:
        r["stage_label"] = STAGE_LABELS.get(r["stage"], r["stage"])
    out["main_currency"] = main
    return out

