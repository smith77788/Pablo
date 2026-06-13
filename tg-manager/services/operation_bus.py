"""
Operation Bus — универсальный механизм постановки операций в очередь.

Заменяет прямые INSERT INTO operation_queue в 20+ handler-файлах.
Предоставляет единый API: submit / cancel / get_status / list_active.

Контракт:
  - Все op_type из OP_REGISTRY проходят через этот модуль
  - Прямые INSERT INTO operation_queue в новых handler'ах — запрещены
  - Существующие прямые INSERT — оставить как есть (инкрементальная миграция)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)

# ── Registry всех типов операций ─────────────────────────────────────────────
# Ключ: op_type (совпадает с op_worker dispatch)
# description: отображается пользователю в очереди
# min_plan: минимальная подписка для выполнения (или None)
# max_retries: количество автоматических повторов при временной ошибке
OP_REGISTRY: dict[str, dict] = {
    "mass_publish": {
        "description": "Массовая публикация",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "bulk_join": {
        "description": "Массовое вступление в каналы",
        "min_plan": "starter",
        "max_retries": 3,
        "icon": "📥",
    },
    "bulk_leave": {
        "description": "Массовый выход из каналов",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "bulk_bot_edit": {
        "description": "Массовое редактирование ботов",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🤖",
    },
    "bulk_create_channels": {
        "description": "Массовое создание каналов",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "📡",
    },
    "bot_factory": {
        "description": "Создание ботов через BotFather",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "🤖",
    },
    "global_presence_channel": {
        "description": "Global Presence — каналы",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_group": {
        "description": "Global Presence — группы",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_bot": {
        "description": "Global Presence — бот",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_package": {
        "description": "Global Presence — пакет",
        "min_plan": "pro",
        "max_retries": 2,
        "icon": "🌍",
    },
    "global_presence_full_package": {
        "description": "Global Presence — полный пакет",
        "min_plan": "enterprise",
        "max_retries": 2,
        "icon": "🌍",
    },
    "strike": {
        "description": "Strike — эшелонированная жалоба",
        "min_plan": "pro",
        "max_retries": 1,
        "icon": "⚡",
    },
    "gift_transfer": {
        "description": "Передача Telegram-подарков",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "🎁",
    },
    "dm_campaign": {
        "description": "DM-кампания",
        "min_plan": "enterprise",
        "max_retries": 1,
        "icon": "📨",
    },
    "network_broadcast": {
        "description": "Сетевая рассылка",
        "min_plan": "enterprise",
        "max_retries": 1,
        "icon": "📢",
    },
    "seed_presence_pack": {
        "description": "Посев постов в Presence Pack",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "🌱",
    },
    "promote_presence_pack": {
        "description": "Назначение бота администратором Presence Pack",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👑",
    },
    "bulk_edit_channels": {
        "description": "Массовое редактирование каналов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✏️",
    },
    "group_import_all": {
        "description": "Импорт групп со всех аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📥",
    },
    "group_announce": {
        "description": "Объявление во все группы аккаунта",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📢",
    },
    "bulk_dm_adhoc": {
        "description": "Рассылка личных сообщений",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📨",
    },
    "bulk_post_to_channel": {
        "description": "Массовая публикация в канал",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "bulk_update_profile": {
        "description": "Массовое обновление профилей",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✏️",
    },
    "bulk_chan_exec": {
        "description": "Bulk username/about для каналов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "✏️",
    },
    "bulk_post_chans": {
        "description": "Публикация поста в каналы аккаунта",
        "min_plan": "starter",
        "max_retries": 2,
        "icon": "📤",
    },
    "channel_import_all": {
        "description": "Импорт каналов со всех аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "📡",
    },
    "check_accounts_health": {
        "description": "Проверка статуса всех аккаунтов",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔍",
    },
    "scan_owned_resources": {
        "description": "Сканирование собственных каналов/групп",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "🔎",
    },
    "promote_all_admins": {
        "description": "Назначение всех аккаунтов администраторами канала",
        "min_plan": "starter",
        "max_retries": 1,
        "icon": "👑",
    },
}


async def submit(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    params: dict[str, Any],
    *,
    total_items: int = 0,
    scheduled_for: Optional[str] = None,
    template_id: Optional[int] = None,
    max_retries: Optional[int] = None,
) -> int:
    """Поставить операцию в очередь. Возвращает op_id.

    Параметры:
      pool         — asyncpg pool
      owner_id     — telegram user_id владельца
      op_type      — тип операции (из OP_REGISTRY)
      params       — словарь параметров операции
      total_items  — общее количество элементов (для прогресс-бара)
      scheduled_for — ISO timestamp запуска (NULL = немедленно)
      template_id  — id шаблона (если применимо)
      max_retries  — переопределить количество повторов (None = из OP_REGISTRY)

    Raises:
      ValueError — если op_type не зарегистрирован в OP_REGISTRY
    """
    if op_type not in OP_REGISTRY:
        raise ValueError(
            f"operation_bus: unknown op_type={op_type!r}. Register in OP_REGISTRY first."
        )

    meta = OP_REGISTRY[op_type]
    retries = max_retries if max_retries is not None else meta.get("max_retries", 3)

    params_json = json.dumps(params, ensure_ascii=False)

    row = await pool.fetchrow(
        """INSERT INTO operation_queue
               (owner_id, op_type, status, params,
                total_items, done_items,
                scheduled_for, template_id, max_retries, created_at)
           VALUES ($1, $2, 'pending', $3::jsonb,
                   $4, 0,
                   $5::timestamptz, $6, $7, NOW())
           RETURNING id""",
        owner_id,
        op_type,
        params_json,
        total_items,
        scheduled_for,
        template_id,
        retries,
    )
    op_id: int = row["id"]
    log.info(
        "operation_bus: submitted op_id=%d op_type=%s owner=%d total_items=%d",
        op_id,
        op_type,
        owner_id,
        total_items,
    )
    return op_id


async def cancel(pool: asyncpg.Pool, op_id: int, owner_id: int) -> bool:
    """Отменить операцию. Возвращает True если операция найдена и отменена.

    Только pending/running операции могут быть отменены.
    Проверяет owner_id для защиты от несанкционированной отмены.
    """
    result = await pool.execute(
        """UPDATE operation_queue
           SET status = 'cancelled', finished_at = NOW()
           WHERE id = $1
             AND owner_id = $2
             AND status IN ('pending', 'running')""",
        op_id,
        owner_id,
    )
    cancelled = str(result).endswith("1")
    if cancelled:
        log.info("operation_bus: op_id=%d cancelled by owner=%d", op_id, owner_id)
    return cancelled


async def get_status(pool: asyncpg.Pool, op_id: int) -> dict | None:
    """Получить статус операции.

    Возвращает dict с полями: id, op_type, status, done_items, total_items,
    created_at, started_at, finished_at, error_msg, result или None если не найдено.
    """
    row = await pool.fetchrow(
        """SELECT id, owner_id, op_type, status,
                  done_items, total_items,
                  created_at, started_at, finished_at,
                  error_msg, result, retry_count, last_error
           FROM operation_queue
           WHERE id = $1""",
        op_id,
    )
    if not row:
        return None

    meta = OP_REGISTRY.get(row["op_type"], {})
    return {
        **dict(row),
        "description": meta.get("description", row["op_type"]),
        "icon": meta.get("icon", "⚙️"),
    }


async def list_active(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 20,
) -> list[dict]:
    """Список активных операций (pending + running) для владельца."""
    rows = await pool.fetch(
        """SELECT id, op_type, status, done_items, total_items,
                  created_at, started_at, scheduled_for
           FROM operation_queue
           WHERE owner_id = $1
             AND status IN ('pending', 'running')
           ORDER BY created_at DESC
           LIMIT $2""",
        owner_id,
        limit,
    )
    result = []
    for row in rows:
        meta = OP_REGISTRY.get(row["op_type"], {})
        result.append(
            {
                **dict(row),
                "description": meta.get("description", row["op_type"]),
                "icon": meta.get("icon", "⚙️"),
            }
        )
    return result


async def list_recent(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 10,
) -> list[dict]:
    """Список завершённых/отменённых операций для истории."""
    rows = await pool.fetch(
        """SELECT id, op_type, status, done_items, total_items,
                  created_at, started_at, finished_at,
                  error_msg, retry_count
           FROM operation_queue
           WHERE owner_id = $1
             AND status IN ('done', 'failed', 'cancelled', 'skipped')
           ORDER BY finished_at DESC NULLS LAST, created_at DESC
           LIMIT $2""",
        owner_id,
        limit,
    )
    result = []
    for row in rows:
        meta = OP_REGISTRY.get(row["op_type"], {})
        result.append(
            {
                **dict(row),
                "description": meta.get("description", row["op_type"]),
                "icon": meta.get("icon", "⚙️"),
            }
        )
    return result


def describe(op_type: str) -> str:
    """Вернуть человекочитаемое описание типа операции."""
    meta = OP_REGISTRY.get(op_type, {})
    icon = meta.get("icon", "⚙️")
    desc = meta.get("description", op_type)
    return f"{icon} {desc}"
