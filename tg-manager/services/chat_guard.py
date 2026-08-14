"""«Модератор чатов» — логика и хранилище (Chatkeeper-подобный функционал).

Нашего бота добавляют администратором в чат, и он:
  • мгновенно удаляет системные уведомления (вошёл/вышел/закрепил/сменил фото…);
  • приветствует новичков (с авто-удалением приветствия);
  • модерирует: бан/мьют/кик/варны, антиспам ссылок и пересылок от не-админов.

Здесь — чистые функции (БД + классификация системных сообщений), без Telegram
API: сетевые действия (delete/ban/restrict) делает роутер bot/handlers/chat_guard.
Так логику можно проверить на живом Postgres без реального Telegram.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

# ── Настройки по умолчанию ───────────────────────────────────────────────────
# Именно они применяются, пока оператор не переопределил их в панели /guard.
# Разумный «из коробки» профиль: как у Chatkeeper — чистим весь системный шум,
# приветствуем новичков, но агрессивный антиспам выключен (чтобы не резать живых).
DEFAULT_SETTINGS: dict[str, Any] = {
    "clean_service": True,     # общий рубильник чистки системных сообщений
    "clean_join": True,        # «X присоединился к группе»
    "clean_leave": True,       # «X покинул группу»
    "clean_other": True,       # закрепления/смена фото/названия/видеочаты и пр.
    "welcome_text": None,      # приветствие новичку (None = не слать)
    "welcome_ttl": 60,         # авто-удалить приветствие через N сек (0 = не удалять)
    "antispam_links": False,   # удалять сообщения со ссылками от не-админов
    "antispam_forward": False, # удалять пересланные сообщения от не-админов
    "max_warns": 3,            # число предупреждений до наказания
    "warn_action": "mute",     # что делать при достижении лимита: mute|ban|kick
    "mute_minutes": 60,        # длительность мьюта (для warn_action=mute и /mute без аргумента)
}

# Content-type'ы системных сообщений Telegram (значения aiogram ContentType).
# Разбиты на группы, чтобы уважать точечные тумблеры clean_join/leave/other.
JOIN_TYPES = {"new_chat_members"}
LEAVE_TYPES = {"left_chat_member"}
OTHER_SERVICE_TYPES = {
    "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created",
    "message_auto_delete_timer_changed", "pinned_message",
    "proximity_alert_triggered",
    "video_chat_scheduled", "video_chat_started", "video_chat_ended",
    "video_chat_participants_invited",
    "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "write_access_allowed", "boost_added", "giveaway_created",
}
ALL_SERVICE_TYPES = JOIN_TYPES | LEAVE_TYPES | OTHER_SERVICE_TYPES


def merge_settings(raw: Any) -> dict[str, Any]:
    """Слить сохранённые настройки с дефолтами (недостающие ключи → дефолт)."""
    out = dict(DEFAULT_SETTINGS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                out[k] = v
    return out


def should_delete_service(content_type: str, settings: dict[str, Any]) -> bool:
    """Нужно ли удалять системное сообщение данного типа при этих настройках.

    Чистая функция — сердце «мгновенного удаления уведомлений». Уважает общий
    рубильник clean_service и точечные clean_join/clean_leave/clean_other.
    """
    if not settings.get("clean_service", True):
        return False
    if content_type in JOIN_TYPES:
        return bool(settings.get("clean_join", True))
    if content_type in LEAVE_TYPES:
        return bool(settings.get("clean_leave", True))
    if content_type in OTHER_SERVICE_TYPES:
        return bool(settings.get("clean_other", True))
    return False


# ── Хранилище чатов под охраной ──────────────────────────────────────────────

async def register_chat(pool: asyncpg.Pool, owner_id: int, chat_id: int,
                        title: str = "", username: str = "") -> dict[str, Any]:
    """Поставить чат под охрану (или переактивировать/обновить мету).

    Идемпотентно: повторная выдача админки не сбрасывает уже настроенные
    settings — только реактивирует и освежает title/username.
    """
    row = await pool.fetchrow(
        """INSERT INTO guard_chats(owner_id, chat_id, title, username, is_active, settings)
           VALUES($1,$2,$3,$4,TRUE,$5::jsonb)
           ON CONFLICT (chat_id) DO UPDATE SET
             is_active=TRUE,
             title=COALESCE(NULLIF(EXCLUDED.title,''), guard_chats.title),
             username=COALESCE(NULLIF(EXCLUDED.username,''), guard_chats.username),
             updated_at=now()
           RETURNING id, owner_id, chat_id, title, username, is_active, settings""",
        owner_id, chat_id, title or "", username or "", json.dumps(DEFAULT_SETTINGS))
    d = dict(row)
    d["settings"] = merge_settings(d.get("settings"))
    return d


async def deactivate_chat(pool: asyncpg.Pool, chat_id: int) -> None:
    """Снять чат с охраны (бота убрали/разжаловали) — настройки сохраняем."""
    await pool.execute(
        "UPDATE guard_chats SET is_active=FALSE, updated_at=now() WHERE chat_id=$1",
        chat_id)


async def get_chat(pool: asyncpg.Pool, chat_id: int) -> dict[str, Any] | None:
    """Вернуть чат под охраной (с влитыми настройками) или None."""
    row = await pool.fetchrow(
        "SELECT id, owner_id, chat_id, title, username, is_active, settings "
        "FROM guard_chats WHERE chat_id=$1", chat_id)
    if not row:
        return None
    d = dict(row)
    d["settings"] = merge_settings(d.get("settings"))
    return d


async def is_guarded(pool: asyncpg.Pool, chat_id: int) -> dict[str, Any] | None:
    """Активна ли охрана чата: вернуть чат если is_active, иначе None."""
    d = await get_chat(pool, chat_id)
    if d and d.get("is_active"):
        return d
    return None


async def list_chats(pool: asyncpg.Pool, owner_id: int) -> list[dict[str, Any]]:
    """Все чаты под охраной данного оператора (для приватного меню)."""
    rows = await pool.fetch(
        "SELECT id, owner_id, chat_id, title, username, is_active, settings "
        "FROM guard_chats WHERE owner_id=$1 ORDER BY updated_at DESC", owner_id)
    out = []
    for r in rows:
        d = dict(r)
        d["settings"] = merge_settings(d.get("settings"))
        out.append(d)
    return out


async def set_setting(pool: asyncpg.Pool, chat_id: int, key: str,
                      value: Any) -> dict[str, Any]:
    """Изменить один ключ настроек и вернуть свежий полный набор."""
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"неизвестная настройка: {key}")
    cur = await get_chat(pool, chat_id)
    settings = dict(cur["settings"]) if cur else dict(DEFAULT_SETTINGS)
    settings[key] = value
    await pool.execute(
        "UPDATE guard_chats SET settings=$2::jsonb, updated_at=now() WHERE chat_id=$1",
        chat_id, json.dumps(settings))
    return settings


async def toggle_setting(pool: asyncpg.Pool, chat_id: int, key: str) -> dict[str, Any]:
    """Инвертировать булев тумблер настроек. Возвращает свежий набор."""
    cur = await get_chat(pool, chat_id)
    settings = dict(cur["settings"]) if cur else dict(DEFAULT_SETTINGS)
    settings[key] = not bool(settings.get(key))
    await pool.execute(
        "UPDATE guard_chats SET settings=$2::jsonb, updated_at=now() WHERE chat_id=$1",
        chat_id, json.dumps(settings))
    return settings


# ── Предупреждения (/warn) ───────────────────────────────────────────────────

async def add_warning(pool: asyncpg.Pool, chat_id: int, user_id: int) -> int:
    """Добавить предупреждение пользователю в чате. Возвращает новый счётчик."""
    return int(await pool.fetchval(
        """INSERT INTO guard_warnings(chat_id, user_id, warns, updated_at)
           VALUES($1,$2,1,now())
           ON CONFLICT (chat_id, user_id) DO UPDATE SET
             warns=guard_warnings.warns+1, updated_at=now()
           RETURNING warns""",
        chat_id, user_id))


async def get_warnings(pool: asyncpg.Pool, chat_id: int, user_id: int) -> int:
    """Текущее число предупреждений пользователя."""
    return int(await pool.fetchval(
        "SELECT warns FROM guard_warnings WHERE chat_id=$1 AND user_id=$2",
        chat_id, user_id) or 0)


async def reset_warnings(pool: asyncpg.Pool, chat_id: int, user_id: int) -> None:
    """Обнулить предупреждения (снятие /unwarn или после наказания)."""
    await pool.execute(
        "DELETE FROM guard_warnings WHERE chat_id=$1 AND user_id=$2",
        chat_id, user_id)
