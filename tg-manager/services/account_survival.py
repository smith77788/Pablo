"""Выживаемость флота: какую долю аккаунтов владелец теряет и почему.

Разрыв, который это закрывает. Каждая смена статуса аккаунта уже пишется
триггером в `account_status_events`, движок иммунитета считает по ней
автопсию, а бот показывает последние десять смертей. Но главной цифры
направления не было нигде: сколько аккаунтов из каждых ста ушло за неделю и
за месяц, лучше это или хуже прошлой недели, и что именно убивает флот —
свежие аккаунты без прогрева или работа без прокси. Владелец видел список
потерь, но не видел, становится ли лучше.

Что считаем (окна 7 и 30 дней, по уникальным аккаунтам):
  • lost — безвозвратные потери (`LOST_STATUSES`: бан, удаление);
  • restricted — ограничения (`RESTRICTED_STATUSES`: спам-блок, заморозка);
  • recovered — вернулись в строй после ограничения;
  • причины, возраст на момент потери и наличие прокси — по потерям 30 дней;
  • тренд потерь: эта неделя против прошлой.

База доли — флот сейчас (без потерянных) плюс потерянные за окно: удалённые
из базы аккаунты остаются в событиях, и без этого доля занижалась бы ровно у
тех, кто чистит мёртвых.

`summarize` — чистая функция (тесты без БД), `fetch_survival` — сбор из БД.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from services import account_status as _st

log = logging.getLogger(__name__)

# Цели из требований направления: при соблюдении лимитов продукта.
TARGET_LOST_PCT_30 = 3.0
TARGET_RESTRICTED_PCT_30 = 10.0

AGE_BUCKETS = (("fresh", "до 7 дней", 7), ("young", "7–30 дней", 30),
               ("mature", "старше 30 дней", None))


def _pct(part: int, base: int) -> float:
    return round(100.0 * part / base, 1) if base > 0 else 0.0


def _age_bucket(age_days) -> str:
    if age_days is None:
        return "unknown"
    for key, _label, limit in AGE_BUCKETS:
        if limit is None or age_days < limit:
            return key
    return "mature"  # pragma: no cover


def _aware(ts):
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def summarize(events: list, fleet_now: int, now: datetime | None = None) -> dict:
    """Свести события смены статуса в отчёт выживаемости.

    events — строки с ключами acc_id, old_status, new_status, created_at,
    added_at (может быть None: аккаунт удалён из базы), has_proxy (None —
    неизвестно). fleet_now — аккаунты владельца сейчас, кроме потерянных.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    d7, d14 = now - timedelta(days=7), now - timedelta(days=14)

    lost = {7: set(), 30: set()}
    restricted = {7: set(), 30: set()}
    recovered = {7: set(), 30: set()}
    lost_prev7: set = set()
    lost30_info: dict = {}

    for e in events or []:
        ts = _aware(e.get("created_at"))
        if ts is None or ts < now - timedelta(days=30) or ts > now + timedelta(minutes=5):
            continue
        acc = e.get("acc_id")
        new = str(e.get("new_status") or "active").strip().lower()
        old = str(e.get("old_status") or "active").strip().lower()
        in7 = ts >= d7
        if new in _st.LOST_STATUSES:
            lost[30].add(acc)
            if in7:
                lost[7].add(acc)
            # Первая (самая ранняя в окне) потеря аккаунта и есть его смерть.
            prev = lost30_info.get(acc)
            if prev is None or ts < prev["ts"]:
                added = _aware(e.get("added_at"))
                age = (ts - added).days if added else None
                lost30_info[acc] = {"ts": ts, "status": new, "age": age,
                                    "has_proxy": e.get("has_proxy")}
        elif new in _st.RESTRICTED_STATUSES:
            restricted[30].add(acc)
            if in7:
                restricted[7].add(acc)
        elif old in _st.RESTRICTED_STATUSES and not _st.is_dead(new):
            recovered[30].add(acc)
            if in7:
                recovered[7].add(acc)
        if new in _st.LOST_STATUSES and d14 <= ts < d7:
            lost_prev7.add(acc)

    # Аккаунт, потерянный на этой неделе, не считается ещё и «прошлой».
    lost_prev7 -= lost[7]

    fleet_now = max(0, int(fleet_now or 0))
    windows = {}
    for days in (7, 30):
        base = fleet_now + len(lost[days])
        windows[str(days)] = {
            "base": base,
            "lost": len(lost[days]),
            "restricted": len(restricted[days]),
            "recovered": len(recovered[days]),
            "lost_pct": _pct(len(lost[days]), base),
            "restricted_pct": _pct(len(restricted[days]), base),
            "survival_pct": round(100.0 - _pct(len(lost[days]), base), 1),
        }

    causes: dict = {}
    by_age = {k: 0 for k, _l, _d in AGE_BUCKETS}
    by_age["unknown"] = 0
    proxy = {"with_proxy": 0, "no_proxy": 0, "unknown": 0}
    for info in lost30_info.values():
        causes[info["status"]] = causes.get(info["status"], 0) + 1
        by_age[_age_bucket(info["age"])] += 1
        hp = info["has_proxy"]
        proxy["unknown" if hp is None else ("with_proxy" if hp else "no_proxy")] += 1

    this_w, prev_w = len(lost[7]), len(lost_prev7)
    trend = "same" if this_w == prev_w else ("better" if this_w < prev_w else "worse")

    report = {
        "fleet_now": fleet_now,
        "windows": windows,
        "trend": {"this_week": this_w, "prev_week": prev_w, "direction": trend},
        "causes": [{"status": s, "label": _st.ru_label(s), "count": n}
                   for s, n in sorted(causes.items(), key=lambda kv: (-kv[1], kv[0]))],
        "by_age": [{"key": k, "label": label, "count": by_age[k]}
                   for k, label, _d in AGE_BUCKETS]
                  + ([{"key": "unknown", "label": "неизвестно", "count": by_age["unknown"]}]
                     if by_age["unknown"] else []),
        "by_proxy": proxy,
        "targets": {"lost_pct_30": TARGET_LOST_PCT_30,
                    "restricted_pct_30": TARGET_RESTRICTED_PCT_30},
    }
    report["verdict"] = verdict(report)
    return report


def verdict(report: dict) -> dict:
    """Одна строка вывода и уровень: ok / warn / bad / empty."""
    w30 = report["windows"]["30"]
    if w30["base"] == 0:
        return {"level": "empty", "text": "Аккаунтов пока нет — считать нечего."}
    lost_pct, restr_pct = w30["lost_pct"], w30["restricted_pct"]
    over_lost = lost_pct > TARGET_LOST_PCT_30
    over_restr = restr_pct > TARGET_RESTRICTED_PCT_30
    if not over_lost and not over_restr:
        if w30["lost"] == 0 and w30["restricted"] == 0:
            return {"level": "ok", "text": "За 30 дней ни одной потери и ни одного ограничения."}
        return {"level": "ok",
                "text": f"Потери в норме: {lost_pct}% за 30 дней при цели не выше "
                        f"{TARGET_LOST_PCT_30:g}%."}

    # Главный фактор называем только когда он объясняет большинство потерь:
    # иначе совет «прогревайте» звучит при любых данных и перестаёт что-то значить.
    lost_n = w30["lost"]
    hint = ""
    if lost_n:
        fresh = next((a["count"] for a in report["by_age"] if a["key"] == "fresh"), 0)
        no_proxy = report["by_proxy"].get("no_proxy", 0)
        if no_proxy * 2 > lost_n:
            hint = (f" Больше половины потерь ({no_proxy} из {lost_n}) — аккаунты без "
                    "прокси: дайте каждому свой прокси.")
        elif fresh * 2 > lost_n:
            hint = (f" Больше половины потерь ({fresh} из {lost_n}) — аккаунты моложе "
                    "7 дней: не берите их в массовые операции до конца прогрева.")
    if over_lost:
        text = (f"Потери выше нормы: {lost_pct}% флота за 30 дней при цели не выше "
                f"{TARGET_LOST_PCT_30:g}%.{hint}")
        return {"level": "bad", "text": text}
    return {"level": "warn",
            "text": f"Ограничений больше нормы: {restr_pct}% флота за 30 дней при цели "
                    f"не выше {TARGET_RESTRICTED_PCT_30:g}%. Снизьте темп операций."}


_EVENTS_SQL = """
SELECT e.acc_id, e.old_status, e.new_status, e.created_at,
       a.added_at,
       CASE WHEN a.id IS NULL THEN NULL ELSE (a.proxy_id IS NOT NULL) END AS has_proxy
  FROM account_status_events e
  LEFT JOIN tg_accounts a ON a.id = e.acc_id
 WHERE e.owner_id = $1
   AND e.created_at > now() - INTERVAL '30 days'
 ORDER BY e.created_at
 LIMIT 50000
"""


async def fetch_survival(pool, owner_id: int) -> dict:
    """Отчёт выживаемости владельца из БД."""
    lost_sql = _st.sql_status_list(_st.LOST_STATUSES)
    fleet_now = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 "
        f"AND COALESCE(acc_status,'active') NOT IN ({lost_sql})",
        owner_id)
    try:
        rows = await pool.fetch(_EVENTS_SQL, owner_id)
    except Exception as exc:
        # Схема событий могла быть ещё не применена — это не ошибка владельца.
        log.debug("account_survival owner=%s: %s", owner_id, exc)
        rows = []
    report = summarize([dict(r) for r in rows], int(fleet_now or 0))
    report["proxy_downtime"] = await fetch_proxy_downtime(pool, owner_id)
    return report


_DOWNTIME_SQL = """
SELECT COUNT(*) AS outages,
       COUNT(*) FILTER (WHERE ended_at IS NULL) AS open_now,
       COALESCE(SUM(accounts) FILTER (WHERE ended_at IS NULL), 0) AS accounts_now,
       COALESCE(SUM(accounts * EXTRACT(EPOCH FROM (
           LEAST(COALESCE(ended_at, now()), now())
           - GREATEST(started_at, now() - INTERVAL '30 days'))) / 3600.0), 0) AS account_hours
  FROM proxy_outages
 WHERE owner_id = $1
   AND COALESCE(ended_at, now()) > now() - INTERVAL '30 days'
"""


async def fetch_proxy_downtime(pool, owner_id: int) -> dict:
    """Простой аккаунтов из-за мёртвых прокси за 30 дней (schema_v316).

    account_hours — сумма «аккаунт × час» простоя: аккаунты стояли из-за
    прокси, а не из-за себя, и чинить надо прокси.
    """
    try:
        r = await pool.fetchrow(_DOWNTIME_SQL, owner_id)
    except Exception as exc:
        log.debug("proxy_downtime owner=%s: %s", owner_id, exc)
        return {"outages": 0, "open_now": 0, "accounts_now": 0, "account_hours": 0.0}
    return {"outages": int(r["outages"] or 0), "open_now": int(r["open_now"] or 0),
            "accounts_now": int(r["accounts_now"] or 0),
            "account_hours": round(float(r["account_hours"] or 0), 1)}
