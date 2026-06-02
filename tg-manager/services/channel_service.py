"""Channel service — вспомогательные функции для статистики каналов."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def get_channel_stats(
    pool: asyncpg.Pool,
    channel_id: int,
    owner_id: int,
) -> dict:
    """Возвращает статистику канала из БД.

    Поля результата:
        members       — последнее известное кол-во участников (int | None)
        posts_published — кол-во публикаций через нас (int)
        joined_at     — дата добавления канала в систему (datetime | None)
        title         — название канала (str | None)
        username      — @username канала (str | None)
    """
    # Базовые данные из managed_channels
    row = await pool.fetchrow(
        "SELECT title, username, added_at FROM managed_channels "
        "WHERE owner_id=$1 AND channel_id=$2",
        owner_id, channel_id,
    )

    title: Optional[str] = None
    username: Optional[str] = None
    joined_at: Optional[datetime] = None

    if row:
        title = row["title"]
        username = row["username"]
        joined_at = row["added_at"]

    # Кол-во публикаций: из operation_log шагов со статусом ok
    # (target содержит channel_id как строку)
    posts_published: int = 0
    try:
        count_row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM operation_log ol "
            "JOIN operation_queue oq ON oq.id = ol.op_id "
            "WHERE oq.owner_id=$1 AND ol.target=$2 AND ol.status='ok'",
            owner_id, str(channel_id),
        )
        if count_row:
            posts_published = int(count_row["cnt"] or 0)
    except Exception as e:
        log.warning("get_channel_stats: не удалось получить posts_published: %s", e)

    return {
        "members": None,          # берётся из Telethon при необходимости
        "posts_published": posts_published,
        "joined_at": joined_at,
        "title": title,
        "username": username,
    }
