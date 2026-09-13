"""Глобальный губернатор темпа — недостающий актуатор контура обратной связи.

Давление флота (infra_pressure.compute_pressure, 0–100) уже используется как
ЖЁСТКИЙ гейт на входе операции (is_ready_for_op). Но у запущенных операций
тормоза не было: пресс только «пускал/не пускал», а не замедлял. Губернатор
закрывает разрыв — отдаёт живой МНОЖИТЕЛЬ темпа по owner-wide давлению, который
op_worker применяет к паузам между действиями. Один регулятор поверх ВСЕХ
одновременных операций владельца, а не пер-операция.

Множитель кэшируется коротко (TTL), поэтому его можно спрашивать хоть на каждое
действие без нагрузки на БД.
"""
from __future__ import annotations

import logging
import time

import asyncpg

log = logging.getLogger(__name__)

_TTL = 45.0                       # сек: как долго держим множитель в кэше
_CACHE: dict[int, tuple[float, float]] = {}   # owner_id -> (expires_monotonic, mult)

# Пороги давления → множитель паузы. Плавно: 25/40/60/80.
_STEPS = ((80, 4.0), (60, 2.5), (40, 1.6), (25, 1.2))


def multiplier_for_score(score: int) -> float:
    for thr, mult in _STEPS:
        if score >= thr:
            return mult
    return 1.0


def level_for_multiplier(mult: float) -> str:
    if mult >= 2.5:
        return "red"
    if mult > 1.0:
        return "amber"
    return "green"


async def tempo_multiplier(pool: asyncpg.Pool, owner_id: int) -> float:
    """Живой множитель темпа для флота владельца (≥1.0). Кэш на _TTL сек."""
    if not owner_id:
        return 1.0
    now = time.monotonic()
    hit = _CACHE.get(owner_id)
    if hit and hit[0] > now:
        return hit[1]
    mult = 1.0
    try:
        from services.infra_pressure import compute_pressure
        p = await compute_pressure(pool, owner_id)
        mult = multiplier_for_score(int(p.get("score", 0) or 0))
    except Exception as e:
        log.debug("fleet_governor: pressure failed owner=%s: %s", owner_id, e)
    # Anti-Storm — событийный клэмп поверх инерционного давления. Волна банов
    # ловится хуком мгновенно и держит резкий множитель фиксированное окно;
    # берём более сильный из двух, чтобы шторм не «размылся» медленным прессом.
    try:
        from services import anti_storm
        storm_mult = (await anti_storm.current(pool, owner_id)).get("mult", 1.0)
        mult = max(mult, float(storm_mult or 1.0))
    except Exception as e:
        log.debug("fleet_governor: anti_storm failed owner=%s: %s", owner_id, e)
    _CACHE[owner_id] = (now + _TTL, mult)
    return mult


def invalidate(owner_id: int) -> None:
    """Сбросить кэш (напр. после зафиксированного бана — чтобы притормозить сразу)."""
    _CACHE.pop(owner_id, None)


async def status(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Состояние губернатора для UI: уровень, множитель, из чего сложилось."""
    try:
        from services.infra_pressure import compute_pressure
        p = await compute_pressure(pool, owner_id)
    except Exception:
        p = {}
    score = int(p.get("score", 0) or 0)
    mult = multiplier_for_score(score)
    storm = {"level": "calm", "mult": 1.0, "until": None}
    try:
        from services import anti_storm
        storm = await anti_storm.current(pool, owner_id)
    except Exception:
        pass
    storm_mult = float(storm.get("mult", 1.0) or 1.0)
    mult = max(mult, storm_mult)
    level = level_for_multiplier(mult)
    out = {
        "score": score,
        "multiplier": mult,
        "level": level,
        "label": p.get("level_label", "Норма"),
        "emoji": p.get("level_emoji", "🟢"),
        "breakdown": p.get("breakdown", {}),
        "explain": _explain(level, mult),
    }
    if storm_mult > 1.0:
        out["storm"] = storm
        out["explain"] = (
            f"🌩 Anti-Storm: обнаружена волна банов — флот в защитном режиме "
            f"(темп ×{storm_mult:g}). Переждём чистку, чтобы не жечь остальные "
            "аккаунты; режим снимется сам.")
    return out


def _explain(level: str, mult: float) -> str:
    if level == "green":
        return "Флот спокоен — операции идут в полном темпе."
    if level == "amber":
        return (f"Повышенное давление — темп замедлен ×{mult:g} "
                "(паузы между действиями длиннее, чтобы не жечь флот).")
    return (f"Высокое давление — темп резко снижен ×{mult:g}. "
            "Флот бережётся; новые рисковые операции лучше отложить.")
