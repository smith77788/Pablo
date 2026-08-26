"""Рейтинг ботов в поиске Telegram — данные для экрана «Рейтинг» мини-аппа.

ПОЧЕМУ ФАЙЛ ПЕРЕПИСАН. Модуль работал по СВОЕЙ модели данных — «владелец +
канал», с таблицами `tracked_keywords(owner_id, channel_id, check_interval)` и
`search_rankings(owner_id, channel_id, keyword, position, previous_position)`.
Таких колонок в схеме нет и никогда не было: настоящая подсистема рейтинга (её
пишут `ranking_checker`, бот и соседние маршруты мини-аппа) устроена как
«бот + ключевое слово»: `tracked_keywords(bot_id, owner_id, keyword)` и
`search_rankings(keyword_id, bot_id, position, checked_at)`.

Из-за этого КАЖДЫЙ запрос модуля падал, а `except` возвращал пустой список — и
экран «Рейтинг» показывал «Нет ключевых слов» независимо от того, сколько их
добавлено. Добавить ключ с экрана тоже было нельзя: фронт шлёт POST, а маршрут
был зарегистрирован только на GET.

Теперь модуль читает и пишет ту же схему, что и остальная подсистема, поэтому
ключи, добавленные из бота, видны в мини-аппе и наоборот.
"""
from __future__ import annotations

import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# Насколько должна сдвинуться позиция, чтобы поднимать оповещение. Единичное
# дрожание выдачи — не новость, а шум, из-за которого экран оповещений
# становится нечитаемым и его перестают смотреть.
ALERT_MIN_DELTA = 3


async def track_keyword(pool: asyncpg.Pool, owner_id: int, keyword: str,
                        bot_id: Optional[int] = None,
                        region: str = "ru") -> dict:
    """Начать отслеживать ключевое слово для бота владельца.

    bot_id обязателен: позиция считается для КОНКРЕТНОГО бота в выдаче.
    Принадлежность бота проверяется здесь, а не только в обработчике, — чтобы
    чужого бота нельзя было прицепить в обход экрана.
    """
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return {"ok": False, "error": "Укажите ключевое слово"}
    if len(keyword) > 100:
        return {"ok": False, "error": "Ключевое слово слишком длинное"}
    if not bot_id:
        return {"ok": False, "error": "Выберите бота: позиция считается для бота"}
    try:
        owns = await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            int(bot_id), owner_id)
        if not owns:
            return {"ok": False, "error": "Бот не найден"}
        row = await pool.fetchrow(
            """INSERT INTO tracked_keywords (bot_id, owner_id, keyword, region)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (bot_id, keyword)
               DO UPDATE SET is_active = TRUE, region = EXCLUDED.region
               RETURNING id""",
            int(bot_id), owner_id, keyword, (region or "ru")[:16])
        return {"ok": True, "id": row["id"]}
    except Exception as e:
        log.warning("track_keyword error: %s", e)
        return {"ok": False, "error": str(e)}


async def untrack_keyword(pool: asyncpg.Pool, owner_id: int,
                          keyword_id: int) -> dict:
    """Снять ключевое слово с отслеживания (по id, с проверкой владельца)."""
    try:
        res = await pool.execute(
            "DELETE FROM tracked_keywords WHERE id=$1 AND owner_id=$2",
            int(keyword_id), owner_id)
        return {"ok": not (isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0")}
    except Exception as e:
        log.warning("untrack_keyword error: %s", e)
        return {"ok": False, "error": str(e)}


async def get_tracked_keywords(pool: asyncpg.Pool, owner_id: int) -> list:
    """Ключевые слова владельца вместе с именем бота — экран показывает его."""
    try:
        rows = await pool.fetch(
            """SELECT tk.id, tk.bot_id, tk.keyword, tk.is_active, tk.created_at,
                      COALESCE(tk.region, 'ru') AS region,
                      mb.username AS bot_username
                 FROM tracked_keywords tk
                 LEFT JOIN managed_bots mb ON mb.bot_id = tk.bot_id
                WHERE tk.owner_id = $1
                ORDER BY tk.created_at DESC""",
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_tracked_keywords error: %s", e)
        return []


async def record_position(pool: asyncpg.Pool, owner_id: int, keyword_id: int,
                          position: Optional[int]) -> dict:
    """Записать замер позиции и, если сдвиг заметный, поднять оповещение."""
    try:
        kw = await pool.fetchrow(
            "SELECT id, bot_id, keyword FROM tracked_keywords "
            "WHERE id=$1 AND owner_id=$2", int(keyword_id), owner_id)
        if not kw:
            return {"ok": False, "error": "Ключевое слово не найдено"}

        prev = await pool.fetchval(
            "SELECT position FROM search_rankings WHERE keyword_id=$1 "
            "ORDER BY checked_at DESC LIMIT 1", kw["id"])
        await pool.execute(
            "INSERT INTO search_rankings(keyword_id, bot_id, position) "
            "VALUES($1,$2,$3)", kw["id"], kw["bot_id"], position)

        alert = _classify(prev, position)
        if alert:
            await pool.execute(
                """INSERT INTO ranking_alerts
                       (owner_id, keyword_id, bot_id, keyword,
                        old_position, new_position, alert_type)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                owner_id, kw["id"], kw["bot_id"], kw["keyword"],
                prev, position, alert)
        return {"ok": True, "previous_position": prev, "alert": alert}
    except Exception as e:
        log.warning("record_position error: %s", e)
        return {"ok": False, "error": str(e)}


def _classify(prev: Optional[int], cur: Optional[int]) -> Optional[str]:
    """Тип оповещения по смене позиции. None — новость не стоит показа.

    Отдельные типы для «появился в выдаче» и «выпал из неё»: это не движение на
    N позиций, а смена состояния, и для владельца это самое важное событие.
    """
    if prev is None and cur is None:
        return None
    if prev is None:
        return "entered" if cur is not None else None
    if cur is None:
        return "lost"
    delta = prev - cur                      # >0 — поднялся выше (позиция меньше)
    if abs(delta) < ALERT_MIN_DELTA:
        return None
    return "improved" if delta > 0 else "dropped"


async def get_position_history(pool: asyncpg.Pool, owner_id: int,
                               keyword_id: int, limit: int = 30) -> list:
    try:
        rows = await pool.fetch(
            """SELECT sr.position, sr.checked_at
                 FROM search_rankings sr
                 JOIN tracked_keywords tk ON tk.id = sr.keyword_id
                WHERE sr.keyword_id = $1 AND tk.owner_id = $2
                ORDER BY sr.checked_at DESC
                LIMIT $3""",
            int(keyword_id), owner_id, int(limit))
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_position_history error: %s", e)
        return []


async def get_all_positions(pool: asyncpg.Pool, owner_id: int) -> list:
    """Последняя позиция и предыдущая по каждому ключу владельца.

    Предыдущая нужна экрану для стрелки роста/падения. Берём обе одним запросом
    через нумерацию замеров: иначе на каждый ключ уходил бы отдельный рейс до
    базы, а ключей у владельца бывают десятки.
    """
    try:
        rows = await pool.fetch(
            """WITH ranked AS (
                   SELECT sr.keyword_id, sr.position, sr.checked_at,
                          ROW_NUMBER() OVER (PARTITION BY sr.keyword_id
                                             ORDER BY sr.checked_at DESC) AS rn
                     FROM search_rankings sr
                     JOIN tracked_keywords tk ON tk.id = sr.keyword_id
                    WHERE tk.owner_id = $1
               )
               SELECT keyword_id,
                      MAX(position)    FILTER (WHERE rn = 1) AS position,
                      MAX(position)    FILTER (WHERE rn = 2) AS previous_position,
                      MAX(checked_at)  FILTER (WHERE rn = 1) AS last_checked
                 FROM ranked
                WHERE rn <= 2
                GROUP BY keyword_id""",
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_all_positions error: %s", e)
        return []


async def get_alerts(pool: asyncpg.Pool, owner_id: int,
                     unacknowledged_only: bool = False) -> list:
    """Оповещения владельца в форме экрана: type + готовый текст."""
    try:
        where = "owner_id = $1"
        if unacknowledged_only:
            where += " AND acknowledged = FALSE"
        rows = await pool.fetch(
            f"""SELECT id, keyword_id, bot_id, keyword, old_position,
                       new_position, alert_type, created_at, acknowledged
                  FROM ranking_alerts
                 WHERE {where}
                 ORDER BY created_at DESC LIMIT 50""",
            owner_id)
        return [{**dict(r), "type": r["alert_type"],
                 "message": _alert_message(r)} for r in rows]
    except Exception as e:
        log.warning("get_alerts error: %s", e)
        return []


def _alert_message(row) -> str:
    old, new = row["old_position"], row["new_position"]
    kind = row["alert_type"]
    if kind == "entered":
        return f"появился в выдаче на #{new}"
    if kind == "lost":
        return f"выпал из выдачи (был #{old})" if old else "выпал из выдачи"
    if old is None or new is None:
        return "позиция изменилась"
    delta = abs(old - new)
    direction = "вырос" if kind == "improved" else "упал"
    return f"{direction} на {delta}: #{old} → #{new}"


async def acknowledge_alert(pool: asyncpg.Pool, owner_id: int,
                            alert_id: int) -> dict:
    try:
        res = await pool.execute(
            "UPDATE ranking_alerts SET acknowledged = TRUE "
            "WHERE id = $1 AND owner_id = $2", int(alert_id), owner_id)
        return {"ok": not (isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0")}
    except Exception as e:
        log.warning("acknowledge_alert error: %s", e)
        return {"ok": False, "error": str(e)}


async def get_ranking_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Сводка для шапки экрана.

    Каждый показатель считается ОТДЕЛЬНО: раньше весь блок стоял в одном try, и
    падение на любом из них (например, на несуществовавшей ranking_alerts)
    возвращало ошибку вместо всей сводки — шапка была пустой целиком.
    """
    async def _val(sql: str, *args, default=0):
        try:
            return await pool.fetchval(sql, *args)
        except Exception as e:
            log.warning("get_ranking_stats(%s): %s", sql.split()[-1], e)
            return default

    total_tracked = await _val(
        "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE",
        owner_id)
    total_checks = await _val(
        """SELECT COUNT(*) FROM search_rankings sr
             JOIN tracked_keywords tk ON tk.id = sr.keyword_id
            WHERE tk.owner_id = $1""", owner_id)
    avg_position = await _val(
        """SELECT AVG(sr.position) FROM search_rankings sr
             JOIN tracked_keywords tk ON tk.id = sr.keyword_id
            WHERE tk.owner_id = $1
              AND sr.checked_at > NOW() - INTERVAL '7 days'""", owner_id)
    alerts_pending = await _val(
        "SELECT COUNT(*) FROM ranking_alerts WHERE owner_id=$1 AND acknowledged=FALSE",
        owner_id)
    return {
        "total_tracked": total_tracked or 0,
        "total_checks": total_checks or 0,
        "avg_position_7d": round(float(avg_position or 0), 1),
        "alerts_pending": alerts_pending or 0,
    }
