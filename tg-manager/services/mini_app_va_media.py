"""Просмотр и отключение одобренных изображений без раскрытия токена бота."""
from __future__ import annotations

import asyncio
import base64
import io

from aiohttp import web

from services import va_media


class _PhotoBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > va_media.MAX_PHOTO_BYTES:
            raise va_media.MediaError("Фотография слишком большая")
        return super().write(data)


async def download_photo(file_id):
    from aiogram import Bot
    from config import BOT_TOKEN
    bot = Bot(token=BOT_TOKEN)
    try:
        async with asyncio.timeout(20):
            buffer = _PhotoBuffer()
            await bot.download(file_id, destination=buffer)
            data = buffer.getvalue()
            if not data.startswith(b"\xff\xd8\xff"):
                raise va_media.MediaError("Предпросмотр доступен только для фотографий JPEG")
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    finally:
        await bot.session.close()


def setup_routes(app, pool, get_uid):
    async def handle(request):
        owner_id = get_uid(request)
        if not owner_id:
            return web.json_response({"error": "Нет доступа"}, status=401)
        try:
            channel_id = int(request.match_info["cid"])
            media_id = int(request.match_info["mid"]) if "mid" in request.match_info else None
        except (ValueError, TypeError):
            return web.json_response({"error": "Неверный канал или изображение"}, status=400)
        from services.channel_admin import channel_row
        if not await channel_row(pool, owner_id, channel_id):
            return web.json_response({"error": "Канал не найден среди ваших каналов"}, status=404)
        try:
            if media_id is None:
                return web.json_response({"ok": True, "items": await va_media.list_photos(pool, owner_id, channel_id)})
            if request.method == "DELETE":
                await va_media.remove_photo(pool, owner_id, channel_id, media_id)
                return web.json_response({"ok": True})
            photo = await va_media.get_photo(pool, owner_id, channel_id, media_id)
            image = await download_photo(photo["file_id"])
            return web.json_response({"ok": True, "image": image}, headers={"Cache-Control": "no-store"})
        except va_media.MediaError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001 - не раскрываем токен и детали Telegram в ответе
            return web.json_response({"error": "Не удалось загрузить изображение. Попробуйте позже."}, status=503)

    app.router.add_get("/api/miniapp/va/channel/{cid}/media", handle)
    app.router.add_get("/api/miniapp/va/channel/{cid}/media/{mid}", handle)
    app.router.add_delete("/api/miniapp/va/channel/{cid}/media/{mid}", handle)
