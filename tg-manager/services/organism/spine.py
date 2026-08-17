"""Спина организма: общая шина событий + журнал (память) + общий контекст.

То, чего у системы не было — нервная система. Модули больше не изолированы: они
ЭМИТЯТ события в общий поток (emit), а подписчики (мозг, раннер) РЕАГИРУЮТ (on).
Каждое событие ещё и персистится в organism_events — это память («что произошло»)
и durable-шина между процессами. organism_state хранит общий контекст владельца
(активная цель, отклонённые подсказки, тайминги нуджей).

Одна точка правды для «что происходит» и «что мы про это помним».
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# In-process подписчики: kind -> [async handler(owner_id, kind, payload, pool)].
# Ключ "*" — подписка на все события.
_SUBS: dict[str, list] = {}


def on(kind: str, handler) -> None:
    """Подписать in-process обработчик на вид события ("*" — на все)."""
    _SUBS.setdefault(kind, []).append(handler)


def _clear() -> None:  # для тестов
    _SUBS.clear()


async def emit(pool, owner_id: int, kind: str, payload: dict | None = None) -> None:
    """Испустить событие: записать в память (organism_events) и разбудить
    in-process подписчиков. Fail-open — сбой шины не должен ронять вызывающего."""
    payload = payload or {}
    if pool is not None:
        try:
            await pool.execute(
                "INSERT INTO organism_events(owner_id, kind, payload) VALUES($1,$2,$3::jsonb)",
                owner_id, kind, json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            log.debug("spine.emit persist failed kind=%s owner=%s", kind, owner_id)
    for h in list(_SUBS.get(kind, ())) + list(_SUBS.get("*", ())):
        try:
            await h(owner_id, kind, payload, pool)
        except Exception:
            log.debug("spine handler failed kind=%s", kind, exc_info=True)


async def recent_events(pool, owner_id: int, kinds: list[str] | None = None,
                        hours: int = 24, limit: int = 200) -> list[dict]:
    where = "owner_id=$1 AND created_at > now() - ($2 || ' hours')::interval"
    args: list = [owner_id, str(int(hours))]
    if kinds:
        where += " AND kind = ANY($3::text[])"
        args.append(list(kinds))
    rows = await pool.fetch(
        f"SELECT kind, payload, created_at FROM organism_events WHERE {where} "
        f"ORDER BY created_at DESC LIMIT {int(limit)}", *args)
    out = []
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try: p = json.loads(p)
            except Exception: p = {}
        out.append({"kind": r["kind"], "payload": p, "at": r["created_at"]})
    return out


async def event_counts(pool, owner_id: int, hours: int = 24) -> dict[str, int]:
    rows = await pool.fetch(
        "SELECT kind, COUNT(*) AS c FROM organism_events "
        "WHERE owner_id=$1 AND created_at > now() - ($2 || ' hours')::interval "
        "GROUP BY kind", owner_id, str(int(hours)))
    return {r["kind"]: int(r["c"]) for r in rows}


async def prune_events(pool, days: int = 30) -> int:
    """Ретеншн журнала событий: удалить старше N дней (защита от разрастания)."""
    try:
        res = await pool.execute(
            "DELETE FROM organism_events WHERE created_at < now() - ($1 || ' days')::interval",
            str(int(days)))
        return int(str(res).rsplit(" ", 1)[-1]) if str(res).startswith("DELETE") else 0
    except Exception:
        log.debug("spine.prune_events failed")
        return 0


async def state_get(pool, owner_id: int, key: str, default=None):
    row = await pool.fetchrow(
        "SELECT value FROM organism_state WHERE owner_id=$1 AND key=$2", owner_id, key)
    if not row:
        return default
    v = row["value"]
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return default
    return v


async def state_set(pool, owner_id: int, key: str, value) -> None:
    await pool.execute(
        "INSERT INTO organism_state(owner_id, key, value, updated_at) "
        "VALUES($1,$2,$3::jsonb, now()) "
        "ON CONFLICT (owner_id, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
        owner_id, key, json.dumps(value, ensure_ascii=False, default=str))
