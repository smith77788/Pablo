"""Дозор роста — детектор НЕОРГАНИЧЕСКОГО роста подписчиков (накрутки).

Владелец льёт бюджет в рост канала и видит только одно число — «подписчиков
стало больше». Но накрутка выглядит иначе, чем живой приток: тысяча ботов
влетает за день и осыпается через неделю. Живой рост — пологий; накрутка —
вертикальный всплеск с последующим обвалом. Голое число этого не показывает,
и владелец платит за воздух (а Telegram потом может занизить охваты канала с
мёртвой аудиторией).

Дозор смотрит на ряд ежедневных снимков `members_count`
(`channel_member_history`) и ловит:
* **ВСПЛЕСК** — дневной скачок, кратно превышающий обычный темп канала И
  значимый в долях от размера (не «+3 к десяти тысячам»);
* **ОБВАЛ** — такой же резкий минус (осыпание накрутки или отписочная волна).

Здесь только чистый классификатор ряда (его и проверяют тесты) плюс тонкие
обёртки над БД: сводка для мир-снимка и редкий проход, который запоминает
находку в память организма (с дедупом по каналу и дню — один всплеск не звонит
дважды).

Осознанные рамки, чтобы не пугать владельца шумом:
* нужен минимум истории (MIN_POINTS) — иначе базовой линии нет;
* маленькие каналы игнорируем (ABS_FLOOR) — там дневной шум и так велик в долях;
* всплеск = И кратность к обычному темпу, И доля от размера — одно без другого
  ложно (у растущего канала большой абсолютный, но органичный приток).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Сколько снимков нужно, чтобы построить базовую линию обычного темпа.
MIN_POINTS = 5
# Каналы меньше этого размера не тревожим — дневной шум там велик в долях.
ABS_FLOOR = 200
# Всплеск: дневной скачок ≥ SPIKE_MULT × обычного (медианного) дневного шага.
SPIKE_MULT = 5.0
# И одновременно ≥ REL_MIN от размера канала ДО скачка (доля, не абсолют).
REL_MIN = 0.15
# Минимальный абсолютный скачок, ниже которого не тревожим даже на кратности.
MIN_ABS_JUMP = 100

CALM = "calm"
SPIKE = "spike"
CRASH = "crash"

_STATE_KEY = "growth_sentry"


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def classify_series(counts: list[int]) -> dict:
    """Ряд ежедневных значений members_count (по возрастанию даты) → вердикт.

    Возвращает {"level", "delta", "at_index", "pct", "baseline"}. Чистая функция —
    сердце детектора. at_index — индекс снимка, НА котором случился скачок (конец
    шага), чтобы вызывающий сопоставил его с датой ряда.
    """
    clean = [int(c) for c in (counts or []) if c is not None and int(c) >= 0]
    calm = {"level": CALM, "delta": 0, "at_index": None, "pct": 0.0, "baseline": 0.0}
    if len(clean) < MIN_POINTS:
        return calm
    if max(clean) < ABS_FLOOR:
        return calm

    steps = [clean[i] - clean[i - 1] for i in range(1, len(clean))]
    # Базовая линия — медиана АБСОЛЮТНЫХ обычных шагов. Устойчива к единичному
    # выбросу (в отличие от среднего, которое сам всплеск и раздувает).
    baseline = _median([abs(s) for s in steps]) or 1.0

    # Кандидат — шаг с максимальным отклонением по модулю.
    worst_i = max(range(len(steps)), key=lambda i: abs(steps[i]))
    delta = steps[worst_i]
    prev = clean[worst_i]                       # размер ДО скачка
    pct = (abs(delta) / prev) if prev > 0 else 1.0

    big_enough = abs(delta) >= max(MIN_ABS_JUMP, SPIKE_MULT * baseline)
    significant = pct >= REL_MIN
    if big_enough and significant:
        level = SPIKE if delta > 0 else CRASH
        return {"level": level, "delta": delta, "at_index": worst_i + 1,
                "pct": round(pct, 3), "baseline": baseline}
    return calm


def explain(verdict: dict, channel_name: str | None = None) -> str:
    """Человеческое пояснение вердикта — для нуджа/уведомления (по-русски)."""
    name = f"«{channel_name}» " if channel_name else ""
    pct = int(round(verdict.get("pct", 0) * 100))
    d = abs(int(verdict.get("delta", 0)))
    if verdict.get("level") == SPIKE:
        return (f"Канал {name}за день прибавил {d} подписчиков (+{pct}%) — "
                "кратно выше обычного темпа. Похоже на накрутку: боты влетают "
                "разом и осыпаются, а мёртвая аудитория режет охваты. Проверьте "
                "источник прироста.")
    if verdict.get("level") == CRASH:
        return (f"Канал {name}за день потерял {d} подписчиков (−{pct}%) — резкий "
                "обвал. Осыпание накрутки или отписочная волна: стоит понять причину.")
    return ""


# ── Обёртки над БД (тонкие; логика — выше) ─────────────────────────────────

async def _owner_channels(pool, owner_id: int, limit: int = 200) -> list[dict]:
    try:
        rows = await pool.fetch(
            "SELECT channel_id, title FROM managed_channels "
            "WHERE owner_id=$1 LIMIT $2", owner_id, limit)
        return [dict(r) for r in (rows or [])]
    except Exception:
        return []


async def _series(pool, owner_id: int, channel_id: int, days: int = 45) -> list[dict]:
    try:
        rows = await pool.fetch(
            "SELECT captured_on, members_count FROM channel_member_history "
            "WHERE owner_id=$1 AND channel_id=$2 "
            "AND captured_on > CURRENT_DATE - ($3::int) "
            "ORDER BY captured_on ASC", owner_id, channel_id, days)
        return [dict(r) for r in (rows or [])]
    except Exception:
        return []


async def summary(pool, owner_id: int) -> dict:
    """Read-only сводка для мир-снимка: сколько каналов с подозрением на накрутку
    и худший из них. Не пишет и не эмитит — только читает."""
    out = {"suspicious": 0, "fake_top": None, "worst_pct": 0.0}
    if not owner_id:
        return out
    for ch in await _owner_channels(pool, owner_id):
        series = await _series(pool, owner_id, int(ch["channel_id"]))
        v = classify_series([r["members_count"] for r in series])
        if v["level"] in (SPIKE, CRASH):
            out["suspicious"] += 1
            if v["pct"] > out["worst_pct"]:
                out["worst_pct"] = v["pct"]
                out["fake_top"] = ch.get("title") or str(ch["channel_id"])
    return out


async def scan_and_remember(pool, owner_id: int) -> list[dict]:
    """Редкий проход: найти аномалии и записать НОВЫЕ в память организма.

    Дедуп по (channel_id, дата всплеска): один и тот же всплеск не звонит дважды.
    Возвращает список свежих находок. Fail-open.
    """
    if not owner_id:
        return []
    from services.organism import spine
    try:
        seen = await spine.state_get(pool, owner_id, _STATE_KEY, {}) or {}
    except Exception:
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    fresh: list[dict] = []
    for ch in await _owner_channels(pool, owner_id):
        cid = int(ch["channel_id"])
        series = await _series(pool, owner_id, cid)
        v = classify_series([r["members_count"] for r in series])
        if v["level"] not in (SPIKE, CRASH):
            continue
        at = series[v["at_index"]]["captured_on"] if v.get("at_index") is not None else None
        marker = f"{cid}:{at}"
        if seen.get(str(cid)) == str(at):
            continue                       # этот всплеск уже отзвонили
        seen[str(cid)] = str(at)
        finding = {"channel_id": cid, "title": ch.get("title"),
                   "level": v["level"], "delta": v["delta"], "pct": v["pct"],
                   "at": str(at)}
        fresh.append(finding)
        try:
            await spine.emit(pool, owner_id, "growth_" + v["level"],
                             {"channel_id": cid, "delta": v["delta"], "pct": v["pct"]})
        except Exception:
            log.debug("growth_sentry: emit failed owner=%s ch=%s", owner_id, cid)
    if fresh:
        try:
            await spine.state_set(pool, owner_id, _STATE_KEY, seen)
        except Exception:
            log.debug("growth_sentry: state_set failed owner=%s", owner_id)
    return fresh
