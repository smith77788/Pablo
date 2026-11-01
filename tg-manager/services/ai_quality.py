"""Журнал ответов ИИ клиентам и оценка их качества владельцем.

ЗАЧЕМ. Продавец в боте и AI-автоответ разговаривают с живыми клиентами, а
увидеть, ЧТО они отвечают, владелец мог только в самой переписке. Нечем было
и измерить, стало ли лучше после правки персоны или смены модели. Журнал
(ai_reply_log, schema_v263) хранит каждую пару «сообщение клиента → ответ»,
итог проверки (services/sales_guard) и оценку владельца «хорошо / плохо».
Сводка за период — доля оценённых плохо, сработавшие проверки, задержка ответа.

Запись журнала не должна мешать ответу клиенту: сбой записи — предупреждение
в лог, а не исключение (log.warning, не debug: потерянная запись должна быть
видна).
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_MAX_TEXT = 4000
_PAGE_MAX = 100
GUARDS = ("price", "discount", "injection")


def _pool(pool):
    if pool is not None:
        return pool
    from services import ai_gateway
    return ai_gateway._pool()


async def log_reply(*, owner_id: int, source: str, user_text: str, reply: str,
                    source_id: Optional[int] = None, provider: Optional[str] = None,
                    model: Optional[str] = None, guard: Optional[str] = None,
                    guard_detail: Optional[str] = None,
                    latency_ms: Optional[int] = None, pool=None) -> Optional[int]:
    """Записать ответ ИИ. Возвращает id записи или None, если записать не вышло."""
    db = _pool(pool)
    if db is None or not owner_id:
        return None
    if guard is not None and guard not in GUARDS:
        guard = None
    try:
        return await db.fetchval(
            "INSERT INTO ai_reply_log (owner_id, source, source_id, provider, model, "
            "user_text, reply, guard, guard_detail, latency_ms) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            int(owner_id), str(source)[:40], source_id, provider, model,
            (user_text or "")[:_MAX_TEXT], (reply or "")[:_MAX_TEXT], guard,
            (guard_detail or None) and str(guard_detail)[:200],
            int(latency_ms) if latency_ms is not None else None)
    except Exception as exc:  # noqa: BLE001 - журнал не должен ронять ответ клиенту
        log.warning("ai_quality: ответ ИИ не записан в журнал (owner=%s): %s", owner_id, exc)
        return None


async def rate_reply(pool, owner_id: int, reply_id: int, rating: Optional[int]) -> bool:
    """Оценка владельца: 1 — хорошо, -1 — плохо, None — снять оценку.
    Чужую запись не оценить: запрос ограничен owner_id."""
    if rating not in (1, -1, None):
        raise ValueError("оценка — 1, -1 или пусто")
    row = await pool.fetchrow(
        "UPDATE ai_reply_log SET rating=$3 WHERE id=$1 AND owner_id=$2 RETURNING id",
        int(reply_id), int(owner_id), rating)
    return row is not None


async def list_replies(pool, owner_id: int, *, limit: int = 30, offset: int = 0,
                       only: Optional[str] = None) -> dict:
    """Страница журнала, новые сверху. only: 'bad' — оценённые плохо,
    'guard' — где сработала проверка, None — все."""
    limit = max(1, min(int(limit or 30), _PAGE_MAX))
    offset = max(0, int(offset or 0))
    where = "owner_id=$1"
    if only == "bad":
        where += " AND rating = -1"
    elif only == "guard":
        where += " AND guard IS NOT NULL"
    elif only is not None:
        raise ValueError("фильтр журнала — bad, guard или пусто")
    total = await pool.fetchval(f"SELECT COUNT(*) FROM ai_reply_log WHERE {where}",
                                int(owner_id))
    rows = await pool.fetch(
        "SELECT id, source, source_id, provider, model, user_text, reply, guard, "
        "guard_detail, rating, latency_ms, created_at FROM ai_reply_log "
        f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3",
        int(owner_id), limit, offset)
    items = [dict(r) for r in rows]
    total = int(total or 0)
    return {"items": items, "total": total, "offset": offset,
            "remaining": max(0, total - offset - len(items))}


async def summary(pool, owner_id: int, *, days: int = 7) -> dict:
    """Сводка качества за days дней: ответов, оценено хорошо/плохо, сколько раз
    сработала каждая проверка, средняя и 90-я перцентиль задержки."""
    days = max(1, min(int(days or 7), 90))
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS replies, "
        "COUNT(*) FILTER (WHERE rating = 1) AS good, "
        "COUNT(*) FILTER (WHERE rating = -1) AS bad, "
        "COUNT(*) FILTER (WHERE guard = 'price') AS guard_price, "
        "COUNT(*) FILTER (WHERE guard = 'discount') AS guard_discount, "
        "COUNT(*) FILTER (WHERE guard = 'injection') AS guard_injection, "
        "AVG(latency_ms)::int AS latency_avg_ms, "
        "(percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms))::int AS latency_p90_ms "
        "FROM ai_reply_log WHERE owner_id=$1 "
        "AND created_at >= now() - make_interval(days => $2)",
        int(owner_id), days)
    out = {k: (int(row[k]) if row and row[k] is not None else 0)
           for k in ("replies", "good", "bad", "guard_price", "guard_discount",
                     "guard_injection", "latency_avg_ms", "latency_p90_ms")}
    rated = out["good"] + out["bad"]
    out["days"] = days
    out["bad_share"] = round(out["bad"] / rated, 3) if rated else None
    return out
