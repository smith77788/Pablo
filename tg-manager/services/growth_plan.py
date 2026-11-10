"""План роста: цель по каналу или боту, честный прогресс и рычаги на своих данных.

ЗАЧЕМ. Прежний «План роста» спрашивал «сколько подписчиков за сколько дней» и
делил число по вшитым долям: 50% инвайт, 20% самопиар, 20% Growth Agent, 10%
накрутка — одинаково для всех, не глядя ни на то, ЧТО растим, ни на то, чем
владелец располагает. Цель не сохранялась, прогресс не считался, и через
минуту от плана не оставалось ничего.

Теперь цель — это запись: какой канал или бот, с какого числа, до какого и к
какой дате. По ней считается:
  * ПРОГРЕСС — сколько набрано, сколько осталось, какой темп нужен в день и
    какой он на деле (за последние дни), успеваем ли и когда придём при
    текущем темпе;
  * РЫЧАГИ — что реально можно сделать тем, что у владельца ЕСТЬ: тёплая
    аудитория для самопиара (сколько людей ещё не там), ёмкость флота для
    инвайта (сколько приглашений в день тянут аккаунты) и собранная аудитория,
    ведение канала, поиск. У каждого рычага — что готово, чего не хватает и,
    где это честно, оценка вилкой; где оценки нет — так и сказано;
  * СЕГОДНЯ — до трёх дел, которые сильнее всего двигают цель;
  * ЧТО УЖЕ ДЕЛАЛИ — операции роста за срок цели и прирост после них.

Чистые функции (progress, build_levers, today_tasks, after_effect) считают по
переданным числам и проверяются без базы; обёртки ниже собирают числа.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

log = logging.getLogger(__name__)

ASSET_KINDS = ("channel", "bot")
ASSET_RU = {"channel": "канал", "bot": "бот"}
MAX_TARGET = 50_000_000
MAX_DAYS = 730
RECENT_DAYS = 7
SERIES_DAYS = 30
# Оценка отклика тёплой аудитории на самопиар: вилка, а не обещание.
SELF_PROMO_LOW, SELF_PROMO_HIGH = 0.03, 0.10
# Доля приглашённых, которые остаются: вилка вокруг campaign_planner.JOIN_YIELD.
INVITE_LOW, INVITE_HIGH = 0.3, 0.6
CAPACITY_ACCOUNTS_MAX = 200

GROWTH_OPS = ("mass_invite", "niche_growth_post", "ai_comment", "self_promo_blast",
              "boost_subscribers", "boost_bot_starts", "boost_views", "boost_reactions",
              "boost_stories")
OP_RU = {
    "mass_invite": "Инвайтинг", "niche_growth_post": "Посты в нишевых группах",
    "ai_comment": "ИИ-комментарии", "self_promo_blast": "Реклама платформы",
    "boost_subscribers": "Накрутка подписчиков", "boost_bot_starts": "Накрутка запусков бота",
    "boost_views": "Накрутка просмотров", "boost_reactions": "Накрутка реакций",
    "boost_stories": "Накрутка сторис", "run_broadcast": "Самопиар в личку",
    "mass_publish": "Самопиар в каналах",
}


class GoalError(ValueError):
    """Неверные данные цели; текст — для человека, по-русски."""


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    a, b = n % 10, n % 100
    if a == 1 and b != 11:
        return one
    if 2 <= a <= 4 and not 12 <= b <= 14:
        return few
    return many


def _num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _days_ru(n: int) -> str:
    return f"{_num(n)} {_plural(n, 'день', 'дня', 'дней')}"


# ── Прогресс ────────────────────────────────────────────────────────────────

def recent_pace(series: list[tuple[date, int]], today: date, days: int = RECENT_DAYS) -> float | None:
    """Средний прирост в день за последние days дней по ряду (дата, число).

    None — истории не хватает (нужны хотя бы две точки в окне).
    """
    pts = sorted((d, int(v)) for d, v in (series or []) if d >= today - timedelta(days=days))
    if len(pts) < 2:
        return None
    span = (pts[-1][0] - pts[0][0]).days
    if span <= 0:
        return None
    return (pts[-1][1] - pts[0][1]) / span


def progress(*, baseline: int, target: int, current: int, started_on: date, deadline: date,
             today: date, series: list[tuple[date, int]] | None = None) -> dict:
    """Где мы относительно цели. Чистая функция.

    state: done | overdue | stalled | behind | on_track | ahead.
    Отставание меряется от прямой «от старта к цели»: к сегодняшнему дню
    должно было набраться expected.
    """
    baseline, target, current = int(baseline), int(target), int(current)
    need_total = max(1, target - baseline)
    gained = current - baseline
    remaining = max(0, target - current)
    days_total = max(1, (deadline - started_on).days)
    days_passed = max(0, (today - started_on).days)
    days_left = (deadline - today).days
    pct = max(0, min(100, round(gained * 100 / need_total)))
    expected = round(need_total * min(days_passed, days_total) / days_total)
    lag = expected - gained
    pace_avg = (gained / days_passed) if days_passed > 0 else None
    pace_recent = recent_pace(series or [], today)
    pace = pace_recent if pace_recent is not None else pace_avg
    need_per_day = math.ceil(remaining / days_left) if days_left > 0 and remaining else 0
    forecast = None
    if remaining and pace and pace > 0:
        forecast = today + timedelta(days=math.ceil(remaining / pace))
    if remaining == 0:
        state = "done"
    elif days_left <= 0:
        state = "overdue"
    elif pace is not None and pace <= 0 and days_passed >= 3:
        state = "stalled"
    elif lag > max(2, need_total * 0.05):
        state = "behind"
    elif lag < -max(2, need_total * 0.05):
        state = "ahead"
    else:
        state = "on_track"
    out = {
        "baseline": baseline, "target": target, "current": current, "gained": gained,
        "remaining": remaining, "pct": pct, "days_total": days_total,
        "days_passed": days_passed, "days_left": days_left, "expected": expected, "lag": lag,
        "need_per_day": need_per_day,
        "pace": round(pace, 1) if pace is not None else None,
        "pace_is_recent": pace_recent is not None,
        "forecast": forecast.isoformat() if forecast else None, "state": state,
    }
    out["verdict"] = verdict(out)
    return out


STATE_RU = {"done": "Цель достигнута", "overdue": "Срок вышел", "stalled": "Рост стоит",
            "behind": "Отстаём", "on_track": "Идём по плану", "ahead": "Опережаем"}


def verdict(p: dict) -> str:
    """Одна-две фразы по-русски: где мы и что это значит."""
    st = p["state"]
    if st == "done":
        return f"Цель достигнута: {_num(p['current'])} из {_num(p['target'])}."
    if st == "overdue":
        return (f"Срок вышел, не хватило {_num(p['remaining'])}. Продлите срок или "
                "поставьте новую цель — рычаги ниже те же.")
    need = f"Нужно по {_num(p['need_per_day'])} в день"
    pace = p["pace"]
    if st == "stalled":
        return (f"За последние дни прироста нет. До цели {_num(p['remaining'])} за "
                f"{_days_ru(p['days_left'])} — {need.lower()}. Нужен новый приток, см. рычаги.")
    pace_txt = (f", сейчас идёт по {str(pace).replace('.', ',')} в день" if pace is not None
                else ", темп пока не с чего считать")
    if st == "behind":
        tail = ""
        if pace and pace > 0 and p["forecast"]:
            tail = f" При таком темпе цель будет {_date_ru(p['forecast'])} — позже срока."
        return f"Отстаём на {_num(p['lag'])}. {need}{pace_txt}.{tail}"
    if st == "ahead":
        return f"Опережаем план на {_num(-p['lag'])}. {need}{pace_txt}."
    return f"Идём по плану. {need}{pace_txt}."


_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


def _date_ru(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month - 1]}"


def after_effect(series: list[tuple[date, int]], day: date, days: int = 3) -> int | None:
    """Прирост за days дней после day по ряду. None — точек не хватает.

    Это «после», а не «из-за»: за те же дни могли сработать и другие действия.
    """
    pts = dict(series or [])
    before = [d for d in pts if d <= day]
    after = [d for d in pts if day < d <= day + timedelta(days=days)]
    if not before or not after:
        return None
    return int(pts[max(after)]) - int(pts[max(before)])


# ── Рычаги ──────────────────────────────────────────────────────────────────

def build_levers(ctx: dict) -> list[dict]:
    """Рычаги роста по тому, что у владельца есть. Чистая функция.

    ctx: kind, remaining, days_left, promo_reach, promo_campaigns, promo_bots,
    accounts, daily_capacity, parsed_audience, va_enabled, has_username,
    keywords, last_ops {op_type: days_ago}.
    Каждый рычаг: key, title, ready, facts[], estimate (low, high | None),
    estimate_note, missing, action {kind, …}.
    """
    kind = ctx.get("kind")
    remaining = int(ctx.get("remaining") or 0)
    days_left = max(0, int(ctx.get("days_left") or 0))
    last = ctx.get("last_ops") or {}
    out: list[dict] = []

    # 1. Самопиар — тёплая аудитория своих ботов.
    reach = int(ctx.get("promo_reach") or 0)
    lev = {"key": "self_promo", "title": "Самопиар по своей аудитории", "ready": reach > 0,
           "facts": [], "estimate": None, "estimate_note": "", "missing": "",
           "action": {"kind": "self_promo"}}
    if reach > 0:
        where = "ещё не в этом боте" if kind == "bot" else "подписаны на ваших ботов"
        lev["facts"].append(f"{_num(reach)} {_plural(reach, 'человек', 'человека', 'человек')} {where}")
        n = int(ctx.get("promo_campaigns") or 0)
        lev["facts"].append(f"кампаний самопиара по этой цели: {n}" if n
                            else "кампаний самопиара по этой цели ещё не было")
        lev["estimate"] = (max(1, math.floor(reach * SELF_PROMO_LOW)),
                           max(1, math.floor(reach * SELF_PROMO_HIGH)))
        lev["estimate_note"] = ("оценка: обычно откликается 3–10% тёплой аудитории; "
                                "точное число покажет сама кампания")
    else:
        lev["missing"] = ("Нет аудитории для показа: нужен ещё один бот с подписчиками."
                          if int(ctx.get("promo_bots") or 0) <= (1 if kind == "bot" else 0)
                          else "Все подписчики ваших ботов уже здесь — нужен внешний приток.")
    out.append(lev)

    # 2. Инвайтинг — только каналы и группы: в бота пригласить нельзя.
    if kind == "channel":
        accounts = int(ctx.get("accounts") or 0)
        cap = int(ctx.get("daily_capacity") or 0)
        aud = int(ctx.get("parsed_audience") or 0)
        lev = {"key": "invite", "title": "Инвайтинг собранной аудитории",
               "ready": accounts > 0 and cap > 0 and aud > 0, "facts": [], "estimate": None,
               "estimate_note": "", "missing": "", "action": {"kind": "invite"}}
        if accounts:
            lev["facts"].append(f"аккаунтов в работе: {_num(accounts)}, вместе тянут "
                                f"{_num(cap)} приглашений в день")
        if aud:
            lev["facts"].append(f"собранная аудитория: {_num(aud)}")
        if not accounts:
            lev["missing"] = "Нет рабочих аккаунтов — добавьте их в разделе «Аккаунты»."
            lev["action"] = {"kind": "accounts"}
        elif not cap:
            lev["missing"] = "Аккаунты сейчас не тянут ни одного приглашения — им нужен прогрев."
            lev["action"] = {"kind": "accounts"}
        elif not aud:
            lev["missing"] = "Приглашать пока некого — соберите аудиторию парсером."
            lev["action"] = {"kind": "parser"}
        else:
            invites = min(cap * days_left, aud)
            lev["estimate"] = (math.floor(invites * INVITE_LOW), math.floor(invites * INVITE_HIGH))
            lev["estimate_note"] = (f"оценка: до срока флот успеет отправить {_num(invites)} "
                                    "приглашений, остаётся обычно 30–60%")
            lev["action"] = {"kind": "invite", "goal": remaining, "days": max(1, days_left)}
        out.append(lev)

    # 3. Ведение — канал без регулярных постов теряет тех, кого привели.
    if kind == "channel":
        va = bool(ctx.get("va_enabled"))
        out.append({
            "key": "content", "title": "Регулярные публикации", "ready": va,
            "facts": ["канал ведёт виртуальный администратор" if va else
                      "регулярного ведения нет: новые подписчики приходят в тишину и уходят"],
            "estimate": None,
            "estimate_note": "без оценки: публикации удерживают приведённых, а не приводят новых",
            "missing": "" if va else "Включите виртуального администратора — он пишет посты по плану.",
            "action": {"kind": "va"}})

    # 4. Посты в нишевых группах и ИИ-комментарии — холодная аудитория.
    accounts = int(ctx.get("accounts") or 0)
    d_ga, d_ai = last.get("niche_growth_post"), last.get("ai_comment")
    facts = []
    if d_ga is not None:
        facts.append(f"посты в нишевых группах: {_ago(d_ga)}")
    if d_ai is not None:
        facts.append(f"ИИ-комментарии: {_ago(d_ai)}")
    if not facts:
        facts.append("ещё не запускались")
    out.append({
        "key": "growth_agent", "title": "Посты в нишевых группах и ИИ-комментарии",
        "ready": accounts > 0, "facts": facts, "estimate": None,
        "estimate_note": "без оценки: отклик зависит от ниши и текста — смотрите прирост после запуска",
        "missing": "" if accounts else "Нужны рабочие аккаунты.",
        "action": {"kind": "growth_agent" if accounts else "accounts"}})

    # 5. Поиск Telegram.
    kw = int(ctx.get("keywords") or 0)
    seo_facts = [f"отслеживается ключевых слов: {kw}" if kw else "ключевые слова не отслеживаются"]
    if kind == "channel" and not ctx.get("has_username"):
        seo_facts.append("у канала нет @username — в поиске его не найти")
    out.append({
        "key": "seo", "title": "Находимость в поиске Telegram", "ready": True,
        "facts": seo_facts, "estimate": None,
        "estimate_note": "без оценки: разовая настройка, работает постоянно",
        "missing": "", "action": {"kind": "seo"}})

    # 6. Накрутка — соц-доказательство, а не аудитория.
    d_b = min((v for k, v in last.items() if k.startswith("boost_")), default=None)
    out.append({
        "key": "boost", "title": "Накрутка для соц-доказательства", "ready": True,
        "facts": [f"последний запуск: {_ago(d_b)}" if d_b is not None else "ещё не запускалась"],
        "estimate": None,
        "estimate_note": "в цель не считаем: это цифра на витрине, а не живые люди",
        "missing": "", "action": {"kind": "boost"}})
    return out


def _ago(days: int) -> str:
    days = int(days)
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{_days_ru(days)} назад"


def coverage(levers: list[dict], remaining: int) -> dict:
    """Хватает ли оценённых рычагов на остаток цели. Чистая функция."""
    low = sum(lv["estimate"][0] for lv in levers if lv.get("estimate") and lv["ready"])
    high = sum(lv["estimate"][1] for lv in levers if lv.get("estimate") and lv["ready"])
    remaining = int(remaining)
    if remaining <= 0:
        text = ""
    elif high == 0:
        text = ("Рычагов с оценкой сейчас нет — цель держится на том, что оценить нельзя "
                "(поиск, нишевые группы). Подготовьте самопиар или инвайтинг.")
    elif low >= remaining:
        text = (f"Оценённых рычагов хватает с запасом: даже осторожно это +{_num(low)} "
                f"при остатке {_num(remaining)}.")
    elif high >= remaining:
        text = (f"Оценённые рычаги дают от +{_num(low)} до +{_num(high)} при остатке "
                f"{_num(remaining)}: в цель попадём, только если всё сработает хорошо.")
    else:
        text = (f"Оценённые рычаги дают не больше +{_num(high)}, а до цели {_num(remaining)}. "
                "Нужна ещё аудитория, ещё аккаунты или срок побольше.")
    return {"low": low, "high": high, "text": text}


def today_tasks(p: dict, levers: list[dict]) -> list[dict]:
    """До трёх дел на сегодня — готовые рычаги с оценкой вперёд. Чистая функция."""
    if p["state"] == "done":
        return [{"text": "Цель достигнута — поставьте следующую.", "action": {"kind": "new_goal"}}]
    tasks: list[dict] = []
    if p["state"] == "overdue":
        tasks.append({"text": "Срок вышел — продлите его, чтобы план снова считал темп.",
                      "action": {"kind": "edit_goal"}})
    ready = [lv for lv in levers if lv["ready"] and lv.get("estimate")]
    ready.sort(key=lambda lv: -lv["estimate"][1])
    for lv in ready:
        lo, hi = lv["estimate"]
        tasks.append({"text": f"{lv['title']}: от +{_num(lo)} до +{_num(hi)}",
                      "action": lv["action"]})
    for lv in levers:
        if lv["missing"] and lv["key"] in ("self_promo", "invite", "content"):
            tasks.append({"text": lv["missing"], "action": lv["action"]})
    if not tasks:
        tasks.append({"text": "Запустите посты в нишевых группах — это приток новой аудитории.",
                      "action": {"kind": "growth_agent"}})
    return tasks[:3]


# ── База ────────────────────────────────────────────────────────────────────

async def asset_now(pool, owner_id: int, kind: str, asset_id: int) -> dict | None:
    """Канал или бот владельца с текущим числом. None — не его или нет такого."""
    if kind == "channel":
        row = await pool.fetchrow(
            "SELECT channel_id AS id, title, username, COALESCE(members_count, 0) AS current "
            "FROM managed_channels WHERE owner_id=$1 AND channel_id=$2 ORDER BY id LIMIT 1",
            int(owner_id), int(asset_id))
        if not row:
            return None
        name = row["title"] or ("@" + row["username"] if row["username"] else "Канал")
        return {"kind": kind, "id": int(row["id"]), "name": name, "username": row["username"],
                "current": int(row["current"])}
    if kind == "bot":
        row = await pool.fetchrow(
            """SELECT mb.bot_id AS id, mb.username, mb.first_name,
                      (SELECT COUNT(*) FROM bot_users bu WHERE bu.bot_id = mb.bot_id
                          AND bu.is_active AND NOT bu.is_blocked AND NOT bu.suspect) AS current
                 FROM managed_bots mb WHERE mb.added_by=$1 AND mb.bot_id=$2""",
            int(owner_id), int(asset_id))
        if not row:
            return None
        name = "@" + row["username"] if row["username"] else (row["first_name"] or "Бот")
        return {"kind": kind, "id": int(row["id"]), "name": name, "username": row["username"],
                "current": int(row["current"])}
    return None


async def assets(pool, owner_id: int) -> dict:
    """Что можно растить: каналы и боты владельца с текущими числами."""
    oid = int(owner_id)
    ch = await pool.fetch(
        """SELECT * FROM (SELECT DISTINCT ON (channel_id) channel_id AS id, title, username,
                  COALESCE(members_count, 0) AS current
             FROM managed_channels WHERE owner_id=$1 ORDER BY channel_id, id) c
            ORDER BY current DESC LIMIT 200""", oid)
    bots = await pool.fetch(
        """SELECT mb.bot_id AS id, mb.username, mb.first_name,
                  (SELECT COUNT(*) FROM bot_users bu WHERE bu.bot_id = mb.bot_id
                      AND bu.is_active AND NOT bu.is_blocked AND NOT bu.suspect) AS current
             FROM managed_bots mb WHERE mb.added_by=$1 AND mb.is_active=TRUE
            ORDER BY current DESC LIMIT 200""", oid)
    return {
        "channels": [{"id": int(r["id"]), "current": int(r["current"]),
                      "name": r["title"] or ("@" + r["username"] if r["username"] else "Канал")}
                     for r in ch],
        "bots": [{"id": int(r["id"]), "current": int(r["current"]),
                  "name": "@" + r["username"] if r["username"] else (r["first_name"] or "Бот")}
                 for r in bots],
    }


async def series(pool, owner_id: int, kind: str, asset_id: int, *, current: int,
                 since: date, today: date) -> list[tuple[date, int]]:
    """Ряд (дата, число) с даты since. Сегодняшняя точка — текущее число.

    Канал — ежедневные снимки участников. Бот — по дням прихода людей: снимков
    у ботов нет, поэтому ряд собирается назад от текущего числа (уходы тех
    дней в нём не видны — это ряд притока).
    """
    pts: dict[date, int] = {}
    if kind == "channel":
        for r in await pool.fetch(
                "SELECT captured_on, members_count FROM channel_member_history "
                "WHERE owner_id=$1 AND channel_id=$2 AND captured_on >= $3 ORDER BY captured_on",
                int(owner_id), int(asset_id), since):
            pts[r["captured_on"]] = int(r["members_count"])
    else:
        rows = await pool.fetch(
            """SELECT (bu.first_seen AT TIME ZONE 'UTC')::date AS day, COUNT(*) AS n
                 FROM bot_users bu JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                WHERE mb.added_by=$1 AND bu.bot_id=$2
                  AND bu.is_active AND NOT bu.is_blocked AND NOT bu.suspect
                  AND bu.first_seen >= $3::date
                GROUP BY 1 ORDER BY 1""", int(owner_id), int(asset_id), since)
        per_day = {r["day"]: int(r["n"]) for r in rows}
        running = int(current)
        d = today
        while d >= since:
            pts[d] = running
            running -= per_day.get(d, 0)
            d -= timedelta(days=1)
    pts[today] = int(current)
    return sorted(pts.items())


def clean_goal(body: dict, *, current: int) -> dict:
    """Тело запроса → target и days. GoalError — что не так, по-русски."""
    b = body if isinstance(body, dict) else {}
    try:
        target = int(b.get("target"))
        days = int(b.get("days"))
    except (TypeError, ValueError):
        raise GoalError("Укажите цель и срок числами.") from None
    if isinstance(b.get("target"), bool) or isinstance(b.get("days"), bool):
        raise GoalError("Укажите цель и срок числами.")
    if target <= int(current):
        raise GoalError(f"Цель должна быть больше, чем сейчас ({_num(current)}).")
    if target > MAX_TARGET:
        raise GoalError(f"Цель не больше {_num(MAX_TARGET)}.")
    if not 1 <= days <= MAX_DAYS:
        raise GoalError(f"Срок — от 1 до {MAX_DAYS} дней.")
    return {"target": target, "days": days}


async def create_goal(pool, owner_id: int, body: dict, *, today: date | None = None) -> int:
    kind = str((body or {}).get("asset_kind") or "")
    if kind not in ASSET_KINDS:
        raise GoalError("Выберите, что растим: канал или бота.")
    try:
        asset_id = int((body or {}).get("asset_id"))
    except (TypeError, ValueError):
        raise GoalError("Выберите, что растим.") from None
    asset = await asset_now(pool, owner_id, kind, asset_id)
    if not asset:
        raise GoalError("Канал или бот не найден.")
    g = clean_goal(body, current=asset["current"])
    start = today or date.today()
    row = await pool.fetchrow(
        """INSERT INTO growth_asset_goals(owner_id, asset_kind, asset_id, title, baseline, target,
                                    started_on, deadline)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (owner_id, asset_kind, asset_id) WHERE status = 'active' DO NOTHING
           RETURNING id""",
        int(owner_id), kind, asset_id, asset["name"][:120], asset["current"], g["target"],
        start, start + timedelta(days=g["days"]))
    if not row:
        raise GoalError(f"У этого {ASSET_RU[kind]}а уже есть цель — измените её или завершите.")
    return int(row["id"])


async def get_goal(pool, owner_id: int, goal_id) -> dict | None:
    try:
        gid = int(goal_id)
    except (TypeError, ValueError):
        return None
    row = await pool.fetchrow(
        "SELECT * FROM growth_asset_goals WHERE id=$1 AND owner_id=$2", gid, int(owner_id))
    return dict(row) if row else None


async def update_goal(pool, owner_id: int, goal: dict, body: dict, *,
                      today: date | None = None) -> None:
    """Поменять цель и/или срок (дней от сегодня). Старт и база не двигаются."""
    day = today or date.today()
    b = body if isinstance(body, dict) else {}
    try:
        target = int(b["target"]) if b.get("target") not in (None, "") else int(goal["target"])
        days = (int(b["days"]) if b.get("days") not in (None, "")
                else max(1, (goal["deadline"] - day).days))
    except (TypeError, ValueError):
        raise GoalError("Укажите цель и срок числами.") from None
    if target <= int(goal["baseline"]):
        raise GoalError(f"Цель должна быть больше стартового числа ({_num(goal['baseline'])}).")
    if target > MAX_TARGET:
        raise GoalError(f"Цель не больше {_num(MAX_TARGET)}.")
    if not 1 <= days <= MAX_DAYS:
        raise GoalError(f"Срок — от 1 до {MAX_DAYS} дней.")
    await pool.execute(
        "UPDATE growth_asset_goals SET target=$3, deadline=$4, status='active', reached_on=NULL, "
        "updated_at=NOW() WHERE id=$1 AND owner_id=$2",
        int(goal["id"]), int(owner_id), target, day + timedelta(days=days))


async def delete_goal(pool, owner_id: int, goal_id: int) -> bool:
    res = await pool.execute("DELETE FROM growth_asset_goals WHERE id=$1 AND owner_id=$2",
                             int(goal_id), int(owner_id))
    return res != "DELETE 0"


async def _with_progress(pool, owner_id: int, g: dict, today: date) -> dict:
    asset = await asset_now(pool, owner_id, g["asset_kind"], g["asset_id"])
    current = asset["current"] if asset else int(g["baseline"])
    since = min(g["started_on"], today - timedelta(days=SERIES_DAYS))
    try:
        ser = await series(pool, owner_id, g["asset_kind"], g["asset_id"], current=current,
                           since=since, today=today)
    except Exception:
        log.warning("growth_plan: ряд не собран goal=%s", g["id"], exc_info=True)
        ser = [(today, current)]
    p = progress(baseline=g["baseline"], target=g["target"], current=current,
                 started_on=g["started_on"], deadline=g["deadline"], today=today, series=ser)
    if p["state"] == "done" and g["status"] == "active":
        await pool.execute(
            "UPDATE growth_asset_goals SET status='done', reached_on=$3, updated_at=NOW() "
            "WHERE id=$1 AND owner_id=$2 AND status='active'", int(g["id"]), int(owner_id), today)
        g = {**g, "status": "done", "reached_on": today}
    return {
        "id": int(g["id"]), "asset_kind": g["asset_kind"], "asset_id": int(g["asset_id"]),
        "name": (asset or {}).get("name") or g["title"] or "—", "gone": asset is None,
        "status": g["status"], "started_on": g["started_on"].isoformat(),
        "deadline": g["deadline"].isoformat(), "deadline_ru": _date_ru(g["deadline"].isoformat()),
        "state_ru": STATE_RU[p["state"]], "progress": p,
        "series": [[d.isoformat(), v] for d, v in ser], "_series": ser, "_asset": asset,
    }


def _public(g: dict) -> dict:
    return {k: v for k, v in g.items() if not k.startswith("_")}


async def list_goals(pool, owner_id: int, *, today: date | None = None) -> list[dict]:
    day = today or date.today()
    rows = await pool.fetch(
        "SELECT * FROM growth_asset_goals WHERE owner_id=$1 AND status <> 'archived' "
        "ORDER BY (status = 'active') DESC, deadline, id LIMIT 50", int(owner_id))
    return [_public(await _with_progress(pool, owner_id, dict(r), day)) for r in rows]


async def _lever_context(pool, owner_id: int, g: dict, p: dict) -> dict:
    oid, kind, aid = int(owner_id), g["asset_kind"], int(g["asset_id"])
    ctx: dict = {"kind": kind, "remaining": p["remaining"], "days_left": p["days_left"],
                 "has_username": bool((g.get("_asset") or {}).get("username"))}

    async def val(sql: str, *args, default=0):
        try:
            return await pool.fetchval(sql, *args) or default
        except Exception:
            log.warning("growth_plan: число для рычага не посчитано", exc_info=True)
            return default

    aud_where = ("FROM bot_users bu JOIN managed_bots mb ON mb.bot_id = bu.bot_id "
                 "WHERE mb.added_by=$1 AND mb.is_active=TRUE AND mb.token IS NOT NULL "
                 "AND bu.is_active AND NOT bu.is_blocked AND NOT bu.suspect")
    if kind == "bot":
        ctx["promo_reach"] = int(await val(
            "SELECT COUNT(DISTINCT bu.user_id) " + aud_where + " AND bu.bot_id <> $2 "
            "AND NOT EXISTS (SELECT 1 FROM bot_users t WHERE t.bot_id=$2 AND t.user_id=bu.user_id)",
            oid, aid))
    else:
        ctx["promo_reach"] = int(await val("SELECT COUNT(DISTINCT bu.user_id) " + aud_where, oid))
    ctx["promo_bots"] = int(await val(
        "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE", oid))
    ctx["promo_campaigns"] = int(await val(
        "SELECT COUNT(*) FROM self_promo_campaigns WHERE owner_id=$1 AND target_kind=$2 "
        "AND target_ref=$3 AND status <> 'archived'", oid, kind, aid))
    try:
        acc_ids = [int(r["id"]) for r in await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active AND session_str IS NOT NULL "
            "ORDER BY id LIMIT $2", oid, CAPACITY_ACCOUNTS_MAX)]
    except Exception:
        acc_ids = []
    ctx["accounts"] = len(acc_ids)
    cap = 0
    if kind == "channel" and acc_ids:
        try:
            from services import flood_engine
            for a in acc_ids:
                cap += int((await flood_engine.recommended_daily_limit(pool, a)).get("limit") or 0)
        except Exception:
            log.warning("growth_plan: ёмкость флота не посчитана owner=%s", oid, exc_info=True)
    ctx["daily_capacity"] = cap
    if kind == "channel":
        ctx["parsed_audience"] = int(await val(
            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences "
            "WHERE owner_id=$1 AND COALESCE(is_bot, FALSE) = FALSE", oid))
        ctx["va_enabled"] = bool(await val(
            "SELECT 1 FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2 AND enabled",
            oid, aid, default=None))
    ctx["keywords"] = int(await val(
        "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active", oid))
    last: dict[str, int] = {}
    try:
        for r in await pool.fetch(
                "SELECT op_type, (CURRENT_DATE - MAX(created_at)::date) AS days_ago "
                "FROM operation_queue WHERE owner_id=$1 AND op_type = ANY($2::text[]) "
                "GROUP BY op_type", oid, list(GROWTH_OPS)):
            last[r["op_type"]] = int(r["days_ago"])
    except Exception:
        log.warning("growth_plan: история операций не прочитана owner=%s", oid, exc_info=True)
    ctx["last_ops"] = last
    return ctx


async def action_log(pool, owner_id: int, since: date, ser: list[tuple[date, int]]) -> list[dict]:
    """Операции роста за срок цели и прирост за три дня после каждой."""
    try:
        rows = await pool.fetch(
            """SELECT id, op_type, label, status, done_items, total_items, created_at
                 FROM operation_queue
                WHERE owner_id=$1 AND created_at >= $2::date
                  AND (op_type = ANY($3::text[]) OR params ? 'self_promo_campaign')
                ORDER BY created_at DESC LIMIT 30""", int(owner_id), since, list(GROWTH_OPS))
    except Exception:
        log.warning("growth_plan: журнал действий не прочитан owner=%s", owner_id, exc_info=True)
        return []
    out = []
    for r in rows:
        day = r["created_at"].date()
        out.append({
            "op_id": int(r["id"]), "what": OP_RU.get(r["op_type"], r["label"] or r["op_type"]),
            "label": r["label"] or "", "status": r["status"],
            "done": int(r["done_items"] or 0), "total": int(r["total_items"] or 0),
            "day": day.isoformat(), "day_ru": _date_ru(day.isoformat()),
            "after": after_effect(ser, day),
        })
    return out


async def plan(pool, owner_id: int, goal: dict, *, today: date | None = None) -> dict:
    """Цель целиком: прогресс, рычаги, дела на сегодня, что уже делали."""
    day = today or date.today()
    g = await _with_progress(pool, owner_id, goal, day)
    p = g["progress"]
    ctx = await _lever_context(pool, owner_id, {**goal, "_asset": g["_asset"]}, p)
    levers = build_levers(ctx)
    out = _public(g)
    out["levers"] = levers
    out["coverage"] = coverage(levers, p["remaining"])
    out["today"] = today_tasks(p, levers)
    out["log"] = await action_log(pool, owner_id, goal["started_on"], g["_series"])
    return out
