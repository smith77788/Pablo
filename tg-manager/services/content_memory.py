"""Content Memory — история опубликованных постов на канал (антиповтор редактора).

ЗАЧЕМ. Антиповтор в services/channel_brain.repetition_check сравнивает черновик с
НЕДАВНИМИ постами КОНКРЕТНОГО канала. Раньше этой истории негде было взять:
operation_log хранит факт публикации, но не тело поста на канал. Этот модуль —
источник recent_texts: пишет тело каждого реально опубликованного поста (из
_exec_mass_publish) и отдаёт последние N для гейта качества.

Owner-scoped и fail-soft в стиле проекта:
  • сбой записи НЕ рушит публикацию (история — вспомогательный сигнал, не сама
    операция);
  • сбой чтения → пустой список. Пустая история означает «сравнивать не с чем»,
    а не «дубликат»: гейт тогда честно молчит, а не выдаёт ложную тревогу.

Тело поста — пользовательский контент; хранится по owner_id и наружу не уходит.
Ключ канала (channel_key) — тот же, что пишет путь публикации: str(channel_id).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Верхняя граница окна истории: антиповтору хватает нескольких десятков последних
# постов, а безлимитный LIMIT из внешнего значения — повод для тяжёлого запроса.
_MAX_WINDOW = 100
# Тело поста ограничено лимитом Telegram; режем с запасом на HTML-разметку.
_MAX_BODY = 8192


async def record_published(
    pool,
    owner_id: int,
    channel_key: str,
    body: str,
    *,
    op_id: int | None = None,
    pillar: str | None = None,
) -> None:
    """Записать тело опубликованного поста в историю канала. Fail-soft: сбой не рушит публикацию."""
    try:
        await pool.execute(
            "INSERT INTO va_channel_posts(owner_id, channel_key, op_id, pillar, body) "
            "VALUES($1,$2,$3,$4,$5)",
            int(owner_id),
            str(channel_key),
            int(op_id) if op_id is not None else None,
            (str(pillar)[:120] if pillar else None),
            str(body or "")[:_MAX_BODY],
        )
    except Exception:
        log.debug("content_memory.record_published failed owner=%s ch=%s",
                  owner_id, channel_key, exc_info=True)


async def recent_texts(
    pool,
    owner_id: int,
    channel_key: str,
    *,
    limit: int = 20,
) -> list[str]:
    """Последние тела постов канала (свежие сверху). Fail-soft: сбой → []."""
    n = max(1, min(int(limit), _MAX_WINDOW))
    try:
        rows = await pool.fetch(
            "SELECT body FROM va_channel_posts "
            "WHERE owner_id=$1 AND channel_key=$2 "
            "ORDER BY published_at DESC, id DESC LIMIT $3",
            int(owner_id), str(channel_key), n,
        )
    except Exception:
        log.debug("content_memory.recent_texts failed owner=%s ch=%s",
                  owner_id, channel_key, exc_info=True)
        return []
    return [r["body"] for r in (rows or []) if r["body"]]


async def recent_texts_for_owner(
    pool,
    owner_id: int,
    *,
    limit: int = 20,
) -> list[str]:
    """Последние тела постов владельца ПО ВСЕМ каналам (свежие сверху). Fail-soft → [].

    Нужно там, где нет одного канала: массовая публикация идёт сразу во все каналы
    аккаунта, поэтому антиповтор на предпросмотре сравнивает черновик с недавними
    постами владельца вообще, а не одного канала.
    """
    n = max(1, min(int(limit), _MAX_WINDOW))
    try:
        rows = await pool.fetch(
            "SELECT body FROM va_channel_posts "
            "WHERE owner_id=$1 "
            "ORDER BY published_at DESC, id DESC LIMIT $2",
            int(owner_id), n,
        )
    except Exception:
        log.debug("content_memory.recent_texts_for_owner failed owner=%s",
                  owner_id, exc_info=True)
        return []
    return [r["body"] for r in (rows or []) if r["body"]]
