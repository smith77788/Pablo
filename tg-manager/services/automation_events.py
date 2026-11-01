"""Общая шина событий автоматизации.

Любое значимое событие продукта пишется ОДНОЙ строкой в `automation_events`:
человек открыл бота, ответил, нажал кнопку, получил тег, оплатил, сделка
сменила этап. Дальше события читают потребители, каждый по своему курсору:

  * исполнитель сценариев (`services/workflow_runtime.py`) — запускает
    сценарии, у которых триггер совпал с событием;
  * доставка исходящих вебхуков и выгрузка в Google Sheets (модуль
    интеграций) — копирует строки в свой outbox триггером на таблице.

Контракт таблицы (owner_id, event_type, bot_id, chat_id, data) общий с
модулем интеграций: колонки не переименовывать.

`emit` никогда не роняет вызывающего: событие — побочный продукт основной
работы (ответа боту, записи тега), и сбой записи в шину не должен срывать
саму работу. Сбой пишется в лог с типом события.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Типы событий, которые продукт пишет сам. Экран выбора триггера показывает
# их подписями ниже; неизвестный тип emit всё равно примет (интеграции и
# входящие вебхуки могут добавлять свои), но экран его не предложит.
EVENT_LABELS: dict[str, str] = {
    "contact.started": "Человек открыл бота (/start)",
    "message.received": "Человек написал боту",
    "button.clicked": "Человек нажал кнопку сценария",
    "contact.tag_added": "Человеку поставили тег",
    "contact.tag_removed": "С человека сняли тег",
    "contact.field_set": "Человек ответил на вопрос сценария",
    "contact.opted_out": "Человек отписался («стоп»)",
    "order.created": "Создан заказ",
    "payment.received": "Получена оплата",
    "deal.created": "Создана сделка",
    "deal.stage_changed": "Сделка сменила этап",
    "keyword.hit": "Найдено ключевое слово в чатах",
    "webhook.received": "Пришёл входящий вебхук",
    "workflow.completed": "Сценарий завершён",
}

# Предел полезной нагрузки: событие — не хранилище. Текст сообщения и
# подобное обрезаются, чтобы шина не росла от длинных переписок.
_MAX_STR = 1000
_MAX_KEYS = 40


def _clip(value: Any, depth: int = 0) -> Any:
    """Сжать полезную нагрузку до разумного размера. ЧИСТАЯ."""
    if depth > 3:
        return None
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_KEYS:
                break
            out[str(k)[:64]] = _clip(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clip(v, depth + 1) for v in list(value)[:_MAX_KEYS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STR]


async def _owner_of_bot(pool, bot_id: int) -> int | None:
    try:
        return await pool.fetchval(
            "SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id)
    except Exception:
        log.debug("automation_events: владелец бота %s не найден", bot_id,
                  exc_info=True)
        return None


async def emit(pool, owner_id: int | None, event_type: str,
               data: dict | None = None, *, bot_id: int | None = None,
               chat_id: int | None = None) -> int | None:
    """Записать событие. Возвращает id строки или None при сбое.

    `owner_id` можно не знать (запись тега в db.add_user_tag знает только
    бота) — тогда владелец берётся из `managed_bots.added_by`. Событие без
    владельца не пишется: читатели скоупят всё по owner_id, ничейное событие
    не увидел бы никто.
    """
    event_type = (event_type or "").strip()[:64]
    if not event_type:
        return None
    try:
        if not owner_id and bot_id:
            owner_id = await _owner_of_bot(pool, int(bot_id))
        if not owner_id:
            return None
        payload = json.dumps(_clip(data or {}), ensure_ascii=False, default=str)
        return await pool.fetchval(
            """INSERT INTO automation_events
                   (owner_id, event_type, bot_id, chat_id, data)
               VALUES ($1, $2, $3, $4, $5::jsonb)
               RETURNING id""",
            int(owner_id), event_type,
            int(bot_id) if bot_id else None,
            int(chat_id) if chat_id else None,
            payload)
    except Exception:
        log.warning("automation_events: событие %s не записано", event_type,
                    exc_info=True)
        return None


async def get_cursor(pool, consumer: str) -> int:
    """Последний прочитанный id потребителя. Новый потребитель начинает с
    ТЕКУЩЕГО конца шины, а не с нуля: иначе первый запуск исполнителя
    разом отыграл бы всю накопленную историю и, например, отправил бы
    приветствие каждому, кто когда-либо открывал бота."""
    row = await pool.fetchrow(
        "SELECT last_id FROM automation_cursors WHERE consumer=$1", consumer)
    if row is not None:
        return int(row["last_id"] or 0)
    tail = await pool.fetchval(
        "SELECT COALESCE(MAX(id), 0) FROM automation_events") or 0
    await pool.execute(
        """INSERT INTO automation_cursors (consumer, last_id)
           VALUES ($1, $2) ON CONFLICT (consumer) DO NOTHING""",
        consumer, int(tail))
    return int(tail)


async def advance_cursor(pool, consumer: str, last_id: int) -> None:
    """Сдвинуть курсор вперёд (назад — никогда)."""
    await pool.execute(
        """INSERT INTO automation_cursors (consumer, last_id, updated_at)
           VALUES ($1, $2, now())
           ON CONFLICT (consumer) DO UPDATE
              SET last_id = GREATEST(automation_cursors.last_id, EXCLUDED.last_id),
                  updated_at = now()""",
        consumer, int(last_id))


async def read_since(pool, last_id: int, limit: int = 200) -> list[dict]:
    """События после `last_id` по порядку.

    Свежие (моложе двух секунд) не отдаются. id из последовательности
    выдаётся при вставке, а видна строка после фиксации: запись с меньшим
    id может стать видна ПОЗЖЕ записи с большим. Курсор, ушедший за большой
    id, такую строку пропустил бы навсегда. Каждая запись в шину — одиночный
    INSERT в автокоммите, так что двух секунд с запасом хватает, чтобы все
    строки с меньшими id успели зафиксироваться.
    """
    rows = await pool.fetch(
        """SELECT id, owner_id, event_type, bot_id, chat_id, data, created_at
             FROM automation_events
            WHERE id > $1
              AND created_at < now() - interval '2 seconds'
            ORDER BY id
            LIMIT $2""",
        int(last_id), int(limit))
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("data"), str):
            try:
                d["data"] = json.loads(d["data"])
            except Exception:
                d["data"] = {}
        out.append(d)
    return out
