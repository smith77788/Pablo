"""Story Manager — публикация историй (Stories) от имени своего аккаунта.

Раздел 12 (Специальные модули → Story Manager) и раздел 3/10 («Работа с
историями») паритета Telegram Expert. Публикует историю (фото/видео по URL) на
СВОЙ аккаунт через Telethon stories.SendStoryRequest. Только собственный контент
на собственный профиль — не рассылка третьим лицам.
"""
from __future__ import annotations

import logging
import random

log = logging.getLogger(__name__)

_MAX_MEDIA_BYTES = 25 * 1024 * 1024  # 25 МБ — разумный потолок для истории
_CONNECT_TIMEOUT = 30.0

# Story period (сек): сколько история «живёт». 48ч — только Premium.
_PERIOD_BY_HOURS = {6: 6 * 3600, 12: 12 * 3600, 24: 24 * 3600, 48: 48 * 3600}


def normalize_period(hours) -> int:
    """Часы жизни истории → секунды. Допустимо 6/12/24/48; иначе 24ч."""
    try:
        h = int(hours)
    except (TypeError, ValueError):
        h = 24
    return _PERIOD_BY_HOURS.get(h, _PERIOD_BY_HOURS[24])


def classify_media(content_type: str, url: str) -> str:
    """Определить тип медиа для истории: 'photo' | 'video'.

    Видео-истории идут как document с video-атрибутами; всё остальное — фото.
    """
    ct = (content_type or "").lower()
    u = (url or "").lower().split("?")[0]
    if ct.startswith("video/") or u.endswith((".mp4", ".mov", ".webm", ".m4v")):
        return "video"
    return "photo"


async def _download(url: str) -> tuple[bytes, str]:
    """Скачать медиа по URL. Возвращает (bytes, content_type). Бросает при
    неверной схеме/слишком большом файле/ошибке сети."""
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL должен начинаться с http:// или https://")
    import aiohttp

    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                raise ValueError(f"URL вернул HTTP {resp.status}")
            ct = resp.headers.get("Content-Type", "")
            data = await resp.read()
    if len(data) > _MAX_MEDIA_BYTES:
        raise ValueError(f"Медиа слишком большое ({len(data)//1024//1024} МБ, лимит 25 МБ)")
    if not data:
        raise ValueError("Пустой ответ по URL")
    return data, ct


def _build_media(client_input_file, media_type: str, content_type: str):
    """Собрать InputMedia* для SendStoryRequest из загруженного файла."""
    from telethon.tl.types import (
        InputMediaUploadedPhoto,
        InputMediaUploadedDocument,
        DocumentAttributeVideo,
        DocumentAttributeFilename,
    )

    if media_type == "video":
        return InputMediaUploadedDocument(
            file=client_input_file,
            mime_type=content_type or "video/mp4",
            attributes=[
                DocumentAttributeVideo(duration=0, w=720, h=1280, supports_streaming=True),
                DocumentAttributeFilename("story.mp4"),
            ],
        )
    return InputMediaUploadedPhoto(file=client_input_file)


async def post_story(
    session_string: str,
    media_url: str,
    caption: str = "",
    period_hours: int = 24,
    _acc: dict | None = None,
) -> dict:
    """Опубликовать историю на СВОЙ аккаунт из media_url (фото/видео).

    Возвращает {ok, status: 'posted'|'cant_post'|'error', error?, reply?}.
    Проверяет право публикации (CanSendStoryRequest) перед отправкой.
    """
    if not session_string or len(session_string.strip()) < 10:
        return {"ok": False, "status": "error", "error": "нет сессии"}
    if not media_url or not media_url.strip():
        return {"ok": False, "status": "error", "error": "укажите ссылку на медиа"}

    from services.account_manager import _make_client
    from telethon.tl.functions.stories import SendStoryRequest, CanSendStoryRequest
    from telethon.tl.types import InputPrivacyValueAllowAll
    from telethon.errors import FloodWaitError

    period = normalize_period(period_hours)
    try:
        data, content_type = await _download(media_url.strip())
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)[:160]}

    media_type = classify_media(content_type, media_url)
    client = _make_client(session_string, _acc)
    try:
        import asyncio

        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            return {"ok": False, "status": "error", "error": "сессия истекла"}

        # Проверка права публиковать историю (Premium/лимиты).
        try:
            can = await asyncio.wait_for(client(CanSendStoryRequest(peer="me")), timeout=15.0)
            # can — CanSendStoryCount / Bool в зависимости от версии; если объект с
            # count==0 или явный False — публиковать нельзя.
            if can is False or getattr(can, "count", 1) == 0:
                return {"ok": False, "status": "cant_post",
                        "error": "нельзя опубликовать историю (нужен Premium или исчерпан лимит)"}
        except Exception as e:
            # Некоторые ошибки прямо говорят о запрете — пробрасываем понятно.
            msg = str(e)
            if "PREMIUM" in msg.upper() or "STORIES" in msg.upper():
                return {"ok": False, "status": "cant_post", "error": msg[:160]}
            # иначе продолжаем — пусть SendStory сам вернёт ошибку

        input_file = await asyncio.wait_for(
            client.upload_file(data, file_name=f"story.{'mp4' if media_type == 'video' else 'jpg'}"),
            timeout=120.0,
        )
        media = _build_media(input_file, media_type, content_type)
        try:
            await asyncio.wait_for(
                client(SendStoryRequest(
                    peer="me",
                    media=media,
                    privacy_rules=[InputPrivacyValueAllowAll()],
                    caption=(caption or "")[:2048] or None,
                    random_id=random.randrange(1, 2**63),
                    period=period,
                )),
                timeout=120.0,
            )
        except FloodWaitError as e:
            return {"ok": False, "status": "error", "error": f"FloodWait {e.seconds}s"}
        return {"ok": True, "status": "posted", "media_type": media_type, "period_hours": period // 3600}
    except Exception as e:
        log.warning("post_story failed: %s", e)
        return {"ok": False, "status": "error", "error": str(e)[:160]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
