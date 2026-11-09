"""Что чаще всего ломается у пользователей — одной таблицей.

ЗАЧЕМ. Жалобы «постоянные ошибки» приходят словами, а в базе лежит точный
ответ: каждая сорвавшаяся операция хранит свой текст ошибки. Но смотреть его
было негде, кроме как по одной операции, — и чинили то, о чём громче написали,
а не то, что срывается чаще всего и у большего числа людей.

Здесь сорвавшиеся операции за период собираются в группы «вид операции +
причина». Причина очищается от номеров, имён и ссылок: «канал @a не найден» и
«канал @b не найден» — одна беда, а не две. Сверху то, что задело больше людей.
"""
from __future__ import annotations

import re
from typing import Any

# Сколько последних сбоев читаем: с запасом на неделю большого флота и без
# риска утащить в память всю историю.
_MAX_ROWS = 20000

_URL = re.compile(r"(?:https?://|t\.me/)\S+", re.I)
_USERNAME = re.compile(r"@[A-Za-z0-9_]{3,}")
_QUOTED = re.compile(r"«[^»]{1,80}»|\"[^\"]{1,80}\"|'[^']{1,80}'")
_NUMBER = re.compile(r"-?\d[\d.,:]*")
_SPACES = re.compile(r"\s+")


def normalize_error(text: Any) -> str:
    """Причина сбоя без частностей — ключ группы. Пустое → «без текста ошибки»."""
    s = str(text or "").strip()
    if not s:
        return "без текста ошибки"
    s = s.splitlines()[0]
    s = _URL.sub("‹ссылка›", s)
    s = _USERNAME.sub("@…", s)
    s = _QUOTED.sub("«…»", s)
    s = _NUMBER.sub("N", s)
    s = _SPACES.sub(" ", s).strip(" .:;,-")
    return s[:160] or "без текста ошибки"


def group_failures(rows, limit: int = 15) -> list[dict]:
    """Сгруппировать строки сбоев (op_type, error, owner_id, id, finished_at).

    Порядок: сначала то, что задело больше пользователей, затем по числу
    сбоев. Один человек, сто раз запустивший заведомо неверное, не должен
    заслонять ошибку, на которую наткнулись двадцать человек по разу."""
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (str(r["op_type"] or ""), normalize_error(r["error"]))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "op_type": key[0], "error": key[1], "count": 0, "_users": set(),
                "last_at": r["finished_at"], "example_id": r["id"],
                "example": str(r["error"] or "").strip()[:300],
            }
        g["count"] += 1
        g["_users"].add(r["owner_id"])
        if r["finished_at"] and (not g["last_at"] or r["finished_at"] > g["last_at"]):
            g["last_at"], g["example_id"] = r["finished_at"], r["id"]
            g["example"] = str(r["error"] or "").strip()[:300]
    out = []
    for g in groups.values():
        g["users"] = len(g.pop("_users"))
        out.append(g)
    out.sort(key=lambda g: (-g["users"], -g["count"], g["op_type"], g["error"]))
    return out[:max(1, int(limit))]


async def top_failures(pool, days: int = 7, limit: int = 15) -> dict:
    """Сводка сбоев операций за `days` суток.

    Возвращает {"days", "finished", "failed", "users_hit", "groups": [...]}.
    `finished` — все завершившиеся операции периода: без него «120 сбоев» не
    прочитать, это 1% или половина."""
    days = max(1, min(int(days), 90))
    totals = await pool.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE status <> 'cancelled') AS finished,
                  COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                  COUNT(DISTINCT owner_id) FILTER (WHERE status = 'failed') AS users_hit
             FROM operation_queue
            WHERE finished_at > now() - make_interval(days => $1)""", days)
    rows = await pool.fetch(
        """SELECT id, owner_id, op_type, finished_at,
                  COALESCE(NULLIF(error_msg, ''), last_error, '') AS error
             FROM operation_queue
            WHERE status = 'failed'
              AND finished_at > now() - make_interval(days => $1)
            ORDER BY finished_at DESC
            LIMIT $2""", days, _MAX_ROWS)
    return {
        "days": days,
        "finished": int(totals["finished"] or 0) if totals else 0,
        "failed": int(totals["failed"] or 0) if totals else 0,
        "users_hit": int(totals["users_hit"] or 0) if totals else 0,
        "groups": group_failures(rows, limit),
    }
