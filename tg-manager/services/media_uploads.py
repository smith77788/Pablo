"""Картинки и видео, загруженные с устройства, — по публичной ссылке.

Поля «ссылка на медиа» в мини-аппе (аватар, фото к посту, вложение шага
сценария) принимали только https-ссылку. Отсюда файл с телефона получает
такую ссылку: /media/u/{token}. Токен длинный и случайный — ссылку нельзя
угадать, но её можно отдать Telegram и своим же загрузчикам по URL.

Тип файла определяется по первым байтам, а не по имени и не по заголовку
клиента: под видом картинки не отдать HTML или скрипт с нашего домена.
"""
from __future__ import annotations

import secrets

MAX_BYTES = 20 * 1024 * 1024          # одна загрузка
OWNER_QUOTA_BYTES = 300 * 1024 * 1024  # всё живое у владельца
OWNER_MAX_FILES = 200
TTL_DAYS = 7
TOKEN_LEN = 32


class UploadRejected(ValueError):
    """Отказ с русским текстом для владельца."""


def sniff_mime(head: bytes) -> str | None:
    """MIME по сигнатуре файла: картинки и видео, остальное — None. ЧИСТАЯ."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"qt"):
            return "video/quicktime"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
            return None  # Telegram и браузеры показывают их нестабильно
        return "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return None


def valid_token(token: str) -> bool:
    return (isinstance(token, str) and len(token) == TOKEN_LEN
            and all(c.isalnum() or c in "-_" for c in token))


def public_url(public_base: str, token: str) -> str:
    return f"{public_base.rstrip('/')}/media/u/{token}"


async def store(pool, owner_id: int, blob: bytes) -> dict:
    """Сохранить файл. {token, mime, size}. UploadRejected — отказ."""
    if not blob:
        raise UploadRejected("Файл пустой")
    if len(blob) > MAX_BYTES:
        raise UploadRejected(f"Файл больше {MAX_BYTES // (1024 * 1024)} МБ")
    mime = sniff_mime(blob[:16])
    if not mime:
        raise UploadRejected("Подойдёт только картинка (JPG, PNG, WebP, GIF) "
                             "или видео (MP4, MOV, WebM)")
    async with pool.acquire() as conn, conn.transaction():
        # Просроченное убираем здесь же: отдельной уборки не нужно, а
        # квота считается только по живому.
        await conn.execute(
            "DELETE FROM media_uploads WHERE created_at < now() - make_interval(days => $1)",
            TTL_DAYS)
        # Блокировка по владельцу: две параллельные загрузки не проскочат
        # квоту обе.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('media_uploads'), $1::int)",
                           owner_id % 2_000_000_000)
        used = await conn.fetchrow(
            "SELECT count(*) AS n, COALESCE(sum(size_bytes), 0) AS bytes "
            "FROM media_uploads WHERE owner_id=$1", owner_id)
        if used["n"] >= OWNER_MAX_FILES or used["bytes"] + len(blob) > OWNER_QUOTA_BYTES:
            raise UploadRejected("Слишком много загруженных файлов за неделю — "
                                 "старые освободят место через несколько дней")
        token = secrets.token_urlsafe(24)[:TOKEN_LEN]
        await conn.execute(
            "INSERT INTO media_uploads (token, owner_id, mime, size_bytes, data) "
            "VALUES ($1, $2, $3, $4, $5)", token, owner_id, mime, len(blob), blob)
    return {"token": token, "mime": mime, "size": len(blob)}


async def load_public(pool, token: str) -> tuple[str, bytes] | None:
    """(mime, данные) живого файла или None.

    Доступ — по самому токену (ссылка-ключ, как адрес входящего вебхука):
    владельца тут нет и быть не должно, Telegram качает файл без входа."""
    if not valid_token(token):
        return None
    row = await pool.fetchrow(
        "SELECT mime, data FROM media_uploads WHERE token=$1 "
        "AND created_at >= now() - make_interval(days => $2)", token, TTL_DAYS)
    return (row["mime"], bytes(row["data"])) if row else None
