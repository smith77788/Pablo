"""Одобренная медиатека канала: без чужих фото, случайных картинок и частых повторов."""
from __future__ import annotations

import re

CAPTION_LIMIT = 900  # Запас для служебной подписи платформы и UTF-16 Telegram.
MAX_PHOTO_BYTES = 5 * 1024 * 1024


class MediaError(ValueError):
    pass


def caption_fits(text: str) -> bool:
    return len(text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT


def _words(text: str) -> set[str]:
    return {word[:5] for word in re.findall(r"[а-яёa-z]{4,}", text.casefold())
            if word not in {"этого", "этот", "наших", "нашей", "нашего", "которые", "чтобы",
                            "фото", "фотография", "изображение", "канал", "пост", "можно"}}


async def add_photo(pool, owner_id, channel_id, file_id, file_unique_id, description):
    from services.channel_admin import channel_row
    if not await channel_row(pool, owner_id, channel_id):
        raise MediaError("Канал не найден среди ваших каналов")
    description = description.strip()
    if not 10 <= len(description) <= 500:
        raise MediaError("Опишите изображение: от 10 до 500 символов")
    if not file_id or not file_unique_id:
        raise MediaError("Нужна фотография, отправленная этому боту")
    return await pool.fetchrow(
        "INSERT INTO va_media(owner_id,channel_id,file_id,file_unique_id,description) "
        "VALUES($1,$2,$3,$4,$5) ON CONFLICT(owner_id,channel_id,file_unique_id) "
        "DO UPDATE SET file_id=EXCLUDED.file_id,description=EXCLUDED.description,enabled=TRUE "
        "RETURNING id,description", int(owner_id), int(channel_id), file_id, file_unique_id, description,
    )


async def list_photos(pool, owner_id, channel_id):
    rows = await pool.fetch(
        "SELECT id,description FROM va_media WHERE owner_id=$1 AND channel_id=$2 AND enabled "
        "AND EXISTS(SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2) "
        "ORDER BY id DESC LIMIT 100", int(owner_id), int(channel_id),
    )
    return [dict(row) for row in rows]


async def get_photo(pool, owner_id, channel_id, media_id):
    row = await pool.fetchrow(
        "SELECT id,file_id,description FROM va_media WHERE id=$1 AND owner_id=$2 "
        "AND channel_id=$3 AND enabled AND EXISTS(SELECT 1 FROM managed_channels "
        "WHERE owner_id=$2 AND channel_id=$3)", int(media_id), int(owner_id), int(channel_id),
    )
    if not row:
        raise MediaError("Изображение недоступно или больше не одобрено владельцем")
    return dict(row)


async def remove_photo(pool, owner_id, channel_id, media_id):
    await get_photo(pool, owner_id, channel_id, media_id)
    await pool.execute(
        "UPDATE va_media SET enabled=FALSE WHERE id=$1 AND owner_id=$2 AND channel_id=$3",
        int(media_id), int(owner_id), int(channel_id),
    )


async def has_photos(pool, owner_id, channel_id):
    return bool(await pool.fetchrow(
        "SELECT id FROM va_media WHERE owner_id=$1 AND channel_id=$2 AND enabled LIMIT 1",
        int(owner_id), int(channel_id),
    ))


async def choose_photo(pool, owner_id, channel_id, text):
    if not caption_fits(text):
        return None
    words = _words(text)
    if not words:
        return None
    rows = await pool.fetch(
        "SELECT id,description FROM va_media WHERE owner_id=$1 AND channel_id=$2 AND enabled "
        "AND (selected_at IS NULL OR selected_at < now() - interval '7 days') "
        "AND EXISTS(SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2) "
        "ORDER BY selected_at NULLS FIRST,id LIMIT 100", int(owner_id), int(channel_id),
    )
    ranked = sorted(rows, key=lambda row: len(words & _words(row["description"])), reverse=True)
    for row in ranked:
        if not words & _words(row["description"]):
            break
        # Условный UPDATE резервирует фото и при одновременной генерации двух постов.
        chosen = await pool.fetchrow(
            "UPDATE va_media SET selected_at=now() WHERE id=$1 AND owner_id=$2 AND channel_id=$3 "
            "AND enabled AND (selected_at IS NULL OR selected_at < now() - interval '7 days') "
            "RETURNING id,file_id,description", row["id"], int(owner_id), int(channel_id),
        )
        if chosen:
            return dict(chosen)
    return None


async def draft_photo(pool, owner_id, draft_id):
    row = await pool.fetchrow(
        "SELECT channel_id,media_id FROM va_admin_drafts WHERE id=$1 AND owner_id=$2",
        int(draft_id), int(owner_id),
    )
    if row and row.get("media_id"):
        return await get_photo(pool, owner_id, row["channel_id"], row["media_id"])
    return None


def require_download(params, media_bytes):
    if params.get("require_media") and not media_bytes:
        return {"status": "failed", "summary": "Не удалось загрузить одобренное изображение. Пост не отправлен."}
    return None
