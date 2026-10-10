"""«Нотариус» — чистое ядро заверения рекламных размещений.

Задача: превратить ряд наблюдений «пост на месте / поста нет / посмотреть не
удалось» в вывод, за который не стыдно перед второй стороной сделки.

Здесь нет ни БД, ни Telethon — только правила. Именно они решают, будет продукт
стоить денег или нет, поэтому главные из них сформулированы как запреты.

**Запрет первый: несостоявшееся наблюдение — не улика.** Если аккаунт не смог
прочитать канал (сеть, флуд, мёртвый прокси), это говорит о НАС, а не о канале.
Такое наблюдение (`unknown`) не приближает вердикт «снял раньше срока» ни на
шаг. Нотариус, обвиняющий из-за собственной сетевой ошибки, хуже, чем его
отсутствие: один ложный протокол стоит дороже, чем сто верных.

**Запрет второй: одно отсутствие ничего не доказывает.** API Telegram
периодически отдаёт пустоту там, где пост есть. Снятие засчитывается только по
двум подряд подтверждённым отсутствиям.

**Запрет третий: не выдумывать точность.** Мы не знаем минуту снятия — знаем
ОКНО между последним «пост на месте» и первым «поста нет». Протокол так и
пишет. Честный интервал убедительнее выдуманной точки: любая проверка нашей
цифры подтвердит её, а не поймает нас на подгонке.

**Запрет четвёртый: мало данных — нет вывода.** При покрытии ниже половины
вердикт всегда «недостаточно наблюдений», даже если картина «очевидна».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# ── Состояния наблюдения ───────────────────────────────────────────────────
PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"

# ── Вердикты ───────────────────────────────────────────────────────────────
V_RUNNING = "running"              # срок ещё идёт, пост на месте
V_KEPT = "kept"                    # отвисел обещанное
V_EARLY = "early_removal"          # снят раньше срока
V_NEVER = "never_appeared"         # не появился вовсе
V_INCONCLUSIVE = "inconclusive"    # наблюдений не хватает для вывода

VERDICT_LABEL = {
    V_RUNNING: "⏳ Размещение идёт",
    V_KEPT: "✅ Срок выдержан",
    V_EARLY: "❌ Снят раньше срока",
    V_NEVER: "🚫 Пост не публиковался",
    V_INCONCLUSIVE: "❔ Недостаточно наблюдений",
}

VERDICT_HINT = {
    V_RUNNING: "Наблюдение продолжается — протокол будет закрыт по истечении срока.",
    V_KEPT: "Канал выполнил условие: пост находился на месте весь обещанный срок.",
    V_EARLY: "Основание требовать возврат или дополнительное размещение: "
             "протокол фиксирует, когда пост исчез.",
    V_NEVER: "За всё обещанное окно пост в канале не появлялся ни разу.",
    V_INCONCLUSIVE: "Слишком много неудачных наблюдений — выводу верить нельзя. "
                    "Проверьте прокси наблюдателей и повторите.",
}

# Снятие засчитывается только после стольких подряд подтверждённых отсутствий.
CONFIRM_ABSENT = 2

# Доля состоявшихся наблюдений, ниже которой вывод не делается.
MIN_COVERAGE = 0.5

# Минимум наблюдений, без которого говорить не о чем.
MIN_OBSERVATIONS = 3


def _aware(dt: datetime | None) -> datetime | None:
    """Приводит к UTC-aware. Наивные даты из БД сравнивать с aware нельзя —
    это TypeError прямо в середине вердикта."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Пейсинг наблюдений ─────────────────────────────────────────────────────
# Плотно в начале, редко потом: подмена и досрочное снятие почти всегда
# происходят в первые часы — там и нужна минутная разрешающая способность.
# Дальше частые проверки дают только нагрузку на флот, не давая точности.
_SCHEDULE = (
    (timedelta(hours=1), timedelta(minutes=5)),
    (timedelta(hours=6), timedelta(minutes=15)),
    (timedelta(hours=24), timedelta(minutes=30)),
)
_SLOW = timedelta(hours=1)
# После истечения срока делаем ещё несколько редких проверок: канал, снявший
# пост через минуту после дедлайна, формально прав, и это тоже факт.
_TAIL = timedelta(hours=2)


def plan_next_check(
    *, now: datetime, promised_from: datetime, promised_until: datetime
) -> datetime | None:
    """Когда заглянуть в канал в следующий раз. None — наблюдение закончено."""
    now = _aware(now)
    promised_from = _aware(promised_from)
    promised_until = _aware(promised_until)
    if now >= promised_until + _TAIL:
        return None
    # До начала обещанного окна ждём его начала: раньше смотреть не на что,
    # но и пропускать начало нельзя — на нём держится вердикт «не публиковался».
    if now < promised_from:
        return promised_from
    age = now - promised_from
    step = _SLOW
    for bound, interval in _SCHEDULE:
        if age < bound:
            step = interval
            break
    return now + step


# ── Сведение наблюдений в факты ────────────────────────────────────────────

def _sorted(observations) -> list[dict]:
    out = []
    for o in observations or []:
        try:
            at = _aware(o["observed_at"])
            st = (o.get("state") or "").strip().lower()
        except (AttributeError, KeyError, TypeError):
            continue
        if at is None or st not in (PRESENT, ABSENT, UNKNOWN):
            continue
        out.append({"observed_at": at, "state": st, "views": o.get("views")})
    out.sort(key=lambda x: x["observed_at"])
    return out


def _confirmed_absence(obs: list[dict]) -> datetime | None:
    """Момент ПЕРВОГО отсутствия в подтверждённой серии, либо None.

    `unknown` серию не рвёт и в неё не засчитывается: оно ничего не говорит о
    канале. Серию рвёт только `present` — пост снова на месте.
    """
    streak = 0
    started: datetime | None = None
    for o in obs:
        if o["state"] == ABSENT:
            streak += 1
            if started is None:
                started = o["observed_at"]
            if streak >= CONFIRM_ABSENT:
                return started
        elif o["state"] == PRESENT:
            streak = 0
            started = None
        # UNKNOWN — состояние не меняем
    return None


def fold(observations, *, promised_from, promised_until, now) -> dict:
    """Наблюдения → факты и вердикт.

    Возвращает: verdict, first_seen_at, last_seen_at, absent_since,
    removal_window (кортеж «между чем и чем исчез»), held_min_h/held_max_h,
    views_first/views_last, coverage, counts.
    """
    obs = _sorted(observations)
    promised_from = _aware(promised_from)
    promised_until = _aware(promised_until)
    now = _aware(now)

    n_present = sum(1 for o in obs if o["state"] == PRESENT)
    n_absent = sum(1 for o in obs if o["state"] == ABSENT)
    n_unknown = sum(1 for o in obs if o["state"] == UNKNOWN)
    total = len(obs)
    coverage = (n_present + n_absent) / total if total else 0.0

    present = [o for o in obs if o["state"] == PRESENT]
    first_seen = present[0]["observed_at"] if present else None
    last_seen = present[-1]["observed_at"] if present else None
    views = [o["views"] for o in present if isinstance(o.get("views"), int)]
    absent_since = _confirmed_absence(obs)

    # Окно исчезновения: между последним «на месте» и первым «нет».
    # Точки у нас нет и выдумывать её нельзя.
    removal_window = (last_seen, absent_since) if absent_since else None

    held_min = held_max = None
    if first_seen and last_seen:
        held_min = (last_seen - first_seen).total_seconds() / 3600.0
        end = absent_since or (last_seen if absent_since else None)
        held_max = ((end or last_seen) - first_seen).total_seconds() / 3600.0

    verdict = _verdict(
        obs=obs, coverage=coverage, total=total, first_seen=first_seen,
        absent_since=absent_since, promised_from=promised_from,
        promised_until=promised_until, now=now,
    )

    return {
        "verdict": verdict,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "absent_since": absent_since,
        "removal_window": removal_window,
        "held_min_h": held_min,
        "held_max_h": held_max,
        "views_first": views[0] if views else None,
        "views_last": views[-1] if views else None,
        "coverage": round(coverage, 3),
        "n_present": n_present,
        "n_absent": n_absent,
        "n_unknown": n_unknown,
        "n_total": total,
    }


def _verdict(*, obs, coverage, total, first_seen, absent_since,
             promised_from, promised_until, now) -> str:
    # Мало данных или слишком много неудачных наблюдений — молчим. Это
    # НЕ осторожность ради осторожности: вывод по дырявому ряду не выдержит
    # первой же проверки второй стороной, и тогда обесценятся все остальные.
    if total < MIN_OBSERVATIONS or coverage < MIN_COVERAGE:
        return V_INCONCLUSIVE

    if absent_since is not None:
        if first_seen is None:
            # Ни разу не видели пост. Это «не публиковался» только если мы
            # реально смотрели в течение обещанного окна, а не пришли позже.
            watched_from = obs[0]["observed_at"]
            if watched_from <= promised_from + timedelta(minutes=30):
                return V_NEVER
            return V_INCONCLUSIVE
        # Снятие до дедлайна — нарушение; после — законное.
        return V_EARLY if absent_since < promised_until else V_KEPT

    if first_seen is None:
        # Отсутствие не подтверждено и пост не виден — ряд из `unknown`.
        return V_INCONCLUSIVE

    if now < promised_until:
        return V_RUNNING
    return V_KEPT


# ── Подозрение на накрутку просмотров ──────────────────────────────────────
# Осознанно НЕ вердикт, а сигнал с числами: органика растёт быстро в первый час
# и затухает; накрутка даёт ступень — резкий всплеск в одном интервале, часто
# уже после того, как органика должна была затухнуть. Утверждать по одному
# признаку «здесь боты» мы не вправе, а показать ступень покупателю — вправе.

BURST_RATE_FACTOR = 10.0     # во сколько раз всплеск выше обычного темпа
BURST_MIN_AGE = timedelta(hours=1)


def views_burst(observations) -> dict | None:
    """Ступень в динамике просмотров, либо None."""
    present = [o for o in _sorted(observations)
               if o["state"] == PRESENT and isinstance(o.get("views"), int)]
    if len(present) < 4:
        return None
    start = present[0]["observed_at"]
    rates: list[tuple[float, dict, dict]] = []
    for prev, cur in zip(present, present[1:]):
        hours = (cur["observed_at"] - prev["observed_at"]).total_seconds() / 3600.0
        if hours <= 0:
            continue
        delta = (cur["views"] or 0) - (prev["views"] or 0)
        if delta < 0:
            continue                       # счётчик просмотров не убывает
        rates.append((delta / hours, prev, cur))
    if len(rates) < 3:
        return None
    values = sorted(r[0] for r in rates)
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    if median <= 0:
        return None
    peak_rate, prev, cur = max(rates, key=lambda r: r[0])
    if peak_rate < median * BURST_RATE_FACTOR:
        return None
    # Всплеск в первый час — нормальная органика, а не признак накрутки.
    if cur["observed_at"] - start < BURST_MIN_AGE:
        return None
    return {
        "from": prev["observed_at"],
        "to": cur["observed_at"],
        "gained": (cur["views"] or 0) - (prev["views"] or 0),
        "rate": round(peak_rate),
        "median_rate": round(median, 1),
        "factor": round(peak_rate / median, 1),
    }


# ── Протокол ───────────────────────────────────────────────────────────────

def _fmt(dt: datetime | None) -> str:
    dt = _aware(dt)
    return dt.strftime("%d.%m.%Y %H:%M UTC") if dt else "—"


def certificate_payload(watch: dict, folded: dict) -> str:
    """Каноническая строка протокола — ровно её и подписываем.

    Порядок и состав полей фиксированы: подпись должна проверяться кем угодно,
    а не только нами, иначе она ничего не стоит.
    """
    return "|".join(str(x) for x in (
        watch.get("id"),
        watch.get("channel_ref"),
        watch.get("msg_id"),
        _fmt(watch.get("promised_from")),
        _fmt(watch.get("promised_until")),
        folded.get("verdict"),
        _fmt(folded.get("first_seen_at")),
        _fmt(folded.get("last_seen_at")),
        _fmt(folded.get("absent_since")),
        folded.get("n_present"), folded.get("n_absent"),
        folded.get("n_unknown"), folded.get("coverage"),
    ))


def render_certificate(watch: dict, folded: dict, sig: str | None,
                       burst: dict | None = None) -> str:
    """Человекочитаемый протокол — его показывают второй стороне сделки."""
    v = folded.get("verdict") or V_INCONCLUSIVE
    lines = [
        "ПРОТОКОЛ НАБЛЮДЕНИЯ",
        "",
        f"Канал: {watch.get('channel_title') or watch.get('channel_ref')}",
        f"Пост: {watch.get('msg_id') or '—'}",
    ]
    if watch.get("advertiser"):
        lines.append(f"Рекламодатель: {watch['advertiser']}")
    lines += [
        f"Обещано: с {_fmt(watch.get('promised_from'))} "
        f"по {_fmt(watch.get('promised_until'))}",
        "",
        f"ВЫВОД: {VERDICT_LABEL.get(v, v)}",
        VERDICT_HINT.get(v, ""),
        "",
        "Факты:",
        f"  Впервые зафиксирован: {_fmt(folded.get('first_seen_at'))}",
        f"  Последний раз на месте: {_fmt(folded.get('last_seen_at'))}",
    ]
    win = folded.get("removal_window")
    if win and win[0] and win[1]:
        # Именно интервал, а не выдуманная минута снятия.
        lines.append(f"  Исчез в промежутке: {_fmt(win[0])} — {_fmt(win[1])}")
    if folded.get("held_min_h") is not None:
        lines.append(f"  Подтверждённое удержание: не менее "
                     f"{folded['held_min_h']:.1f} ч")
    if folded.get("views_first") is not None:
        lines.append(f"  Просмотры: {folded.get('views_first')} → "
                     f"{folded.get('views_last')}")
    lines += [
        "",
        f"Наблюдений: {folded.get('n_total')} "
        f"(на месте {folded.get('n_present')}, "
        f"отсутствовал {folded.get('n_absent')}, "
        f"не удалось {folded.get('n_unknown')})",
        f"Покрытие: {int((folded.get('coverage') or 0) * 100)}%",
    ]
    if burst:
        lines += [
            "",
            "⚠️ Подозрение на накрутку просмотров:",
            f"  {_fmt(burst['from'])} — {_fmt(burst['to'])}: "
            f"+{burst['gained']} просмотров "
            f"({burst['factor']}× обычного темпа)",
            "  Это сигнал, а не доказательство: оценивайте вместе с остальным.",
        ]
    if sig:
        lines += ["", f"Подпись: {sig}"]
    return "\n".join(lines)
