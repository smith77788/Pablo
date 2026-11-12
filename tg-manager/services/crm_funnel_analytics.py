"""Аналитика воронки продаж: конверсия, время в стадии, вероятность выигрыша,
застрявшие сделки и прогноз выручки.

ЗАЧЕМ. Воронка (crm_engine.pipeline, доска сделок) показывала СНИМОК: сколько
сделок сейчас в каждой стадии. На вопросы продавца она не отвечала:

* «из десяти лидов сколько доходят до предложения, а сколько до оплаты?»;
* «где сделки застревают — и какие именно застряли сейчас?»;
* «сколько денег придёт из того, что сейчас в работе?».

Посчитать это было не из чего: история переходов не хранилась нигде. Теперь её
пишет триггер (schema_v363, таблица crm_stage_history) на ВСЕХ дверях смены
стадии, а этот модуль её разбирает.

МЕТОДЫ — и почему именно они.

* Конверсия — накопительная: сделка «дошла до стадии», если её самая дальняя
  стадия не ниже этой (перескок «Лид → Переговоры» значит, что «Контакт» и
  «Предложение» пройдены). Иначе перескоки выглядели бы провалом воронки.
* Доля выигранных — по закрытым в окне, с 95% интервалом Уилсона: «3 из 4» и
  «300 из 400» — одинаковые 75%, но верить им можно по-разному, и экран
  обязан это показать.
* Вероятность выигрыша из стадии — по истории САМОГО владельца (сколько
  сделок, дошедших до стадии, в итоге выиграно), со сглаживанием к общей
  доле выигранных (априорное Бета-распределение силой в две сделки), чтобы
  стадия с одной сделкой не давала 0% или 100%. Не «экспертные 10/25/50/75%»,
  которые зашиты у большинства CRM и не значат ничего для конкретного бизнеса.
* Время в стадии — оценкой Каплана–Мейера: сделки, которые СЕЙЧАС стоят в
  стадии, ещё не вышли из неё, и простое среднее по вышедшим систематически
  занижает время (выживают в выборке только быстрые). Каплан–Мейер учитывает
  незавершённые наблюдения как цензурированные — это стандарт анализа
  длительностей.
* Застрявшие — открытые сделки, стоящие в стадии дольше, чем три четверти
  сделок владельца в этой стадии (75-й перцентиль той же оценки). Порог свой
  у каждой стадии и у каждого владельца; когда данных мало (меньше
  MIN_EVENTS выходов из стадии), порогом служит FALLBACK_STUCK_DAYS, и ответ
  это называет.
* Прогноз — сумма открытых сделок, взвешенная вероятностью выигрыша их
  стадии, ПО ВАЛЮТАМ (рубли с долларами не складываются — см. crm_engine).

ЧЕСТНОСТЬ ДАННЫХ. Строки, восстановленные при накате миграции (backfilled),
знают стадию, но не время перехода: в длительностях они не участвуют. Ответ
несёт `history_since` — с какого момента история настоящая, — и экран пишет,
что до этой даты время в стадиях неизвестно.

Чистая часть (`analyze` и помощники) проверяется без базы; запросы — тестом
против настоящей схемы (tests/test_crm_funnel_analytics_postgres.py).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Порядок воронки — тот же список, что у всего проекта (crm_engine.STAGE_LABELS).
OPEN_STAGES = ("lead", "contact", "proposal", "negotiation")
WON, LOST = "won", "lost"
FUNNEL = OPEN_STAGES + (WON,)
_ORD = {s: i for i, s in enumerate(FUNNEL)}

MIN_EVENTS = 5             # меньше выходов из стадии — свой порог не считаем
FALLBACK_STUCK_DAYS = 14.0  # порог «застряла», пока своих данных мало
PRIOR_STRENGTH = 2.0       # сила априорного распределения (в сделках)
STUCK_LIMIT = 20
WINDOWS = (30, 90, 180, 365)
DEFAULT_WINDOW = 90


def _utc(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _days(td: timedelta) -> float:
    return max(td.total_seconds(), 0.0) / 86400.0


# ── Статистика ──────────────────────────────────────────────────────────────

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% интервал Уилсона для доли k/n; None, если n == 0."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def kaplan_meier(durations: list[tuple[float, bool]]) -> list[tuple[float, float]]:
    """Кривая дожития: [(время, доля ещё не вышедших после него)].

    durations — (длительность, вышла_ли). Вышедшие и цензурированные в одно
    время: сначала выходы, потом цензура (стандартное соглашение).
    """
    pts = sorted(durations, key=lambda x: (x[0], not x[1]))
    at_risk = len(pts)
    s = 1.0
    curve: list[tuple[float, float]] = []
    i = 0
    while i < len(pts):
        t = pts[i][0]
        events = censored = 0
        while i < len(pts) and pts[i][0] == t:
            if pts[i][1]:
                events += 1
            else:
                censored += 1
            i += 1
        if events and at_risk:
            s *= 1 - events / at_risk
            curve.append((t, s))
        at_risk -= events + censored
    return curve


def km_quantile(curve: list[tuple[float, float]], q: float) -> float | None:
    """Время, к которому вышла доля q (медиана — q=0.5). None — не достигнуто:
    больше 1−q сделок ещё стоят в стадии, и честного числа нет."""
    for t, s in curve:
        if s <= 1 - q + 1e-12:
            return t
    return None


def _round(v, n=1):
    return None if v is None else round(v, n)


# ── Разбор истории ──────────────────────────────────────────────────────────

def _deal_paths(deals, transitions):
    """Для каждой сделки: упорядоченные переходы."""
    by_key: dict = {}
    for t in transitions:
        by_key.setdefault((t["deal_kind"], str(t["deal_key"])), []).append(t)
    for v in by_key.values():
        v.sort(key=lambda t: (_utc(t["changed_at"]), t.get("id") or 0))
    return by_key


def analyze(deals: list[dict], transitions: list[dict], *, now: datetime,
            window_days: int = DEFAULT_WINDOW, main_currency: str = "RUB") -> dict:
    """Весь отчёт по сделкам и их истории.

    deals — текущие сделки: deal_kind, deal_key, stage, value, currency,
    created_at, name. transitions — строки crm_stage_history этих сделок.
    """
    now = _utc(now)
    since = now - timedelta(days=window_days)
    paths = _deal_paths(deals, transitions)
    real = [t for t in transitions if not t.get("backfilled")]
    history_since = min((_utc(t["changed_at"]) for t in real), default=None)

    reached = {s: 0 for s in FUNNEL}
    cohort = 0
    won_in_window = lost_in_window = 0
    # для вероятности по стадиям: сколько дошедших закрыто и выиграно (за всё время)
    st_closed = {s: 0 for s in OPEN_STAGES}
    st_won = {s: 0 for s in OPEN_STAGES}
    sojourn: dict[str, list[tuple[float, bool]]] = {s: [] for s in OPEN_STAGES}
    cycle_won: list[float] = []
    won_values: dict[str, list[float]] = {}
    open_deals = []

    for d in deals:
        key = (d["deal_kind"], str(d["deal_key"]))
        path = paths.get(key, [])
        stage = d.get("stage") or "lead"
        cur = (d.get("currency") or main_currency or "RUB").upper()

        # самая дальняя стадия воронки, которой сделка достигала
        stages_seen = [stage] + [t["to_stage"] for t in path] + [
            t["from_stage"] for t in path if t.get("from_stage")]
        far = max((_ORD[s] for s in stages_seen if s in _ORD), default=0)
        first_at = _utc(path[0]["changed_at"]) if path else _utc(d.get("created_at"))
        closing = next((t for t in reversed(path) if t["to_stage"] in (WON, LOST)), None)

        if first_at is not None and first_at >= since:
            cohort += 1
            for s in FUNNEL:
                if far >= _ORD[s]:
                    reached[s] += 1

        if stage in (WON, LOST):
            closed_at = _utc(closing["changed_at"]) if closing else None
            if closed_at is not None and closed_at >= since:
                if stage == WON:
                    won_in_window += 1
                else:
                    lost_in_window += 1
            for s in OPEN_STAGES:
                if far >= _ORD[s]:
                    st_closed[s] += 1
                    if stage == WON:
                        st_won[s] += 1
            if stage == WON:
                won_values.setdefault(cur, []).append(float(d.get("value") or 0))
                start = path[0] if path else None
                if start is not None and not start.get("backfilled") and closing is not None:
                    cycle_won.append(_days(_utc(closing["changed_at"]) - _utc(start["changed_at"])))
        else:
            open_deals.append(d)

        # время в стадии: каждый вход с известным временем
        for i, t in enumerate(path):
            s = t["to_stage"]
            if s not in sojourn or t.get("backfilled"):
                continue
            t_in = _utc(t["changed_at"])
            if i + 1 < len(path):
                sojourn[s].append((_days(_utc(path[i + 1]["changed_at"]) - t_in), True))
            elif stage == s:
                sojourn[s].append((_days(now - t_in), False))

    closed_n = won_in_window + lost_in_window
    win_rate = (won_in_window / closed_n) if closed_n else None
    total_closed = sum(1 for d in deals if (d.get("stage") or "lead") in (WON, LOST))
    total_won = sum(1 for d in deals if d.get("stage") == WON)
    prior = (total_won / total_closed) if total_closed else 0.5

    stages = []
    thresholds: dict[str, tuple[float, bool]] = {}
    for idx, s in enumerate(FUNNEL):
        row = {"stage": s, "reached": reached[s]}
        if idx + 1 < len(FUNNEL):
            nxt = reached[FUNNEL[idx + 1]]
            row["to_next"] = _round(nxt / reached[s], 3) if reached[s] else None
        if s in OPEN_STAGES:
            n, k = st_closed[s], st_won[s]
            row["win_probability"] = round((k + PRIOR_STRENGTH * prior) / (n + PRIOR_STRENGTH), 3)
            row["win_probability_n"] = n
            curve = kaplan_meier(sojourn[s])
            events = sum(1 for _, e in sojourn[s] if e)
            med, p75 = km_quantile(curve, 0.5), km_quantile(curve, 0.75)
            row["days_median"] = _round(med)
            row["days_p75"] = _round(p75)
            row["exits"] = events
            row["now_inside"] = sum(1 for _, e in sojourn[s] if not e)
            own = events >= MIN_EVENTS and p75 is not None
            thresholds[s] = (p75 if own else FALLBACK_STUCK_DAYS, own)
            row["stuck_after_days"] = _round(thresholds[s][0])
            row["stuck_threshold_own"] = own
        stages.append(row)

    # застрявшие и прогноз
    prob = {r["stage"]: r.get("win_probability") for r in stages}
    stuck, forecast, pipeline = [], {}, {}
    for d in open_deals:
        stage = d.get("stage") or "lead"
        if stage not in OPEN_STAGES:
            continue
        cur = (d.get("currency") or main_currency or "RUB").upper()
        val = float(d.get("value") or 0)
        pipeline[cur] = pipeline.get(cur, 0.0) + val
        forecast[cur] = forecast.get(cur, 0.0) + val * (prob.get(stage) or 0.0)
        path = paths.get((d["deal_kind"], str(d["deal_key"])), [])
        entered = next((t for t in reversed(path) if t["to_stage"] == stage), None)
        if entered is None or entered.get("backfilled"):
            continue  # когда вошла — неизвестно; не выдумываем
        age = _days(now - _utc(entered["changed_at"]))
        limit, own = thresholds[stage]
        if age > limit:
            stuck.append({
                "deal_kind": d["deal_kind"], "deal_key": str(d["deal_key"]),
                "name": d.get("name") or "", "stage": stage,
                "days": _round(age), "limit_days": _round(limit),
                "threshold_own": own,
                "value": val, "currency": cur,
            })
    stuck.sort(key=lambda x: (-(x["days"] / max(x["limit_days"] or 1, 1)), -x["value"]))

    cycle_curve = sorted(cycle_won)
    cycle_median = cycle_curve[len(cycle_curve) // 2] if cycle_curve else None
    velocity = {}
    if win_rate is not None and cycle_median:
        for cur, vals in won_values.items():
            open_n = sum(1 for d in open_deals
                         if (d.get("currency") or main_currency or "RUB").upper() == cur)
            avg = sum(vals) / len(vals) if vals else 0.0
            velocity[cur] = round(open_n * avg * win_rate / max(cycle_median, 1.0), 2)

    return {
        "window_days": window_days,
        "history_since": history_since.isoformat() if history_since else None,
        "cohort": cohort,
        "stages": stages,
        "win_rate": _round(win_rate, 3),
        "win_rate_ci": [round(x, 3) for x in wilson(won_in_window, closed_n)] if closed_n else None,
        "won": won_in_window, "lost": lost_in_window,
        "cycle_days_median": _round(cycle_median),
        "cycle_n": len(cycle_won),
        "pipeline_by_currency": {k: round(v, 2) for k, v in pipeline.items()},
        "forecast_by_currency": {k: round(v, 2) for k, v in forecast.items()},
        "velocity_per_day_by_currency": velocity,
        "stuck": stuck[:STUCK_LIMIT],
        "stuck_total": len(stuck),
        "open": len(open_deals),
    }


# ── Запросы ─────────────────────────────────────────────────────────────────

async def gather(pool, owner_id: int, main_currency: str) -> tuple[list, list]:
    """Текущие сделки обеих таблиц и их история (только существующих сделок:
    удалённая сделка — не проигранная, и в воронке ей не место)."""
    contacts = await pool.fetch(
        """SELECT 'contact' AS deal_kind, cc.contact_id::text AS deal_key,
                  COALESCE(cc.stage, 'lead') AS stage, cc.deal_value AS value,
                  COALESCE(cc.currency, 'USD') AS currency, cc.created_at,
                  COALESCE(NULLIF(uc.display_name, ''),
                           NULLIF(TRIM(COALESCE(uc.first_name, '') || ' ' || COALESCE(uc.last_name, '')), ''),
                           '@' || NULLIF(uc.username, ''), 'Без имени') AS name
             FROM contact_crm cc
             JOIN unified_contacts uc ON uc.id = cc.contact_id AND uc.owner_id = cc.owner_id
            WHERE cc.owner_id = $1""",
        owner_id)
    simple = await pool.fetch(
        """SELECT 'deal' AS deal_kind, d.id::text AS deal_key,
                  COALESCE(d.stage, 'lead') AS stage, d.value,
                  COALESCE(d.currency, $2) AS currency, d.created_at,
                  COALESCE(NULLIF(d.title, ''), 'Сделка без названия') AS name
             FROM crm_deals d WHERE d.owner_id = $1""",
        owner_id, main_currency)
    hist = await pool.fetch(
        """SELECT h.id, h.deal_kind, h.deal_key, h.from_stage, h.to_stage,
                  h.changed_at, h.backfilled
             FROM crm_stage_history h
            WHERE h.owner_id = $1
              AND ((h.deal_kind = 'contact' AND EXISTS (
                       SELECT 1 FROM contact_crm c
                        WHERE c.owner_id = h.owner_id AND c.contact_id::text = h.deal_key))
                OR (h.deal_kind = 'deal' AND EXISTS (
                       SELECT 1 FROM crm_deals d
                        WHERE d.owner_id = h.owner_id AND d.id::text = h.deal_key)))
            ORDER BY h.changed_at, h.id""",
        owner_id)
    return [dict(r) for r in contacts] + [dict(r) for r in simple], [dict(r) for r in hist]


async def report(pool, owner_id: int, window_days: int = DEFAULT_WINDOW) -> dict:
    from services.contacts_hub.crm_engine import STAGE_LABELS
    from services.mini_app_deals_board import owner_main_currency

    if window_days not in WINDOWS:
        window_days = DEFAULT_WINDOW
    main = await owner_main_currency(pool, owner_id)
    deals, hist = await gather(pool, owner_id, main)
    out = analyze(deals, hist, now=datetime.now(timezone.utc),
                  window_days=window_days, main_currency=main)
    for row in out["stages"]:
        row["label"] = STAGE_LABELS.get(row["stage"], row["stage"])
    for row in out["stuck"]:
        row["stage_label"] = STAGE_LABELS.get(row["stage"], row["stage"])
    out["main_currency"] = main
    return out
