"""Уникализация медиа для массовых рассылок — анти-детект.

Проблема: одно и то же фото/видео, отправленное сотням получателей, у Telegram
одинаковое (после его серверной переупаковки хэш контента совпадает) — это
сильный спам-сигнал и лёгкая кластеризация кампании. Модуль делает каждую копию
визуально-неразличимой, но пиксельно- и байт-различной, чтобы серверный хэш
отличался у каждого получателя.

Стратегия:
  • Растровое изображение (JPEG/PNG/WEBP/BMP) — микро-джиттер яркости/контраста
    (±1.5 %, незаметно глазу) + обрезка 0–2 px по случайным краям (меняет размер и,
    значит, контент) + пересохранение со случайным качеством. Такой вариант
    ПЕРЕЖИВАЕТ серверную переупаковку Telegram, потому что отличается сам контент,
    а не только метаданные.
  • Прочее (GIF/видео/документ) — без ffmpeg попиксельно не переработать, поэтому
    байтовый джиттер: для ISO-BMFF (mp4/mov) добавляем пустой `free`-бокс
    (проигрыватели его игнорируют), иначе — короткий случайный хвост. Это меняет
    файловый хэш; переживает доставку, если Telegram не перекодирует контейнер.

Функция `uniquify` НИКОГДА не бросает: при любой ошибке возвращает исходные байты,
чтобы не срывать отправку. Pillow импортируется лениво — его отсутствие не ломает
импорт модуля и просто отключает пиксельный путь (остаётся байтовый).
"""

from __future__ import annotations

import io
import logging
import os
import random

log = logging.getLogger(__name__)

# Всё, что крупнее — не трогаем пиксельно (слишком дорого/рискованно), только байты.
_MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _is_still_image(head: bytes) -> bool:
    """Растровое изображение, которое Pillow умеет переупаковать без потери сути.
    GIF исключён намеренно — Pillow по умолчанию роняет анимацию."""
    if head[:3] == b"\xff\xd8\xff":                       # JPEG
        return True
    if head[:8] == b"\x89PNG\r\n\x1a\n":                  # PNG
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":     # WEBP
        return True
    if head[:2] == b"BM":                                 # BMP
        return True
    return False


def _uniquify_image(data: bytes) -> bytes:
    """Пиксельный джиттер + пересохранение. Возвращает НОВЫЕ байты того же формата.
    Бросает при неоткрываемом/битом изображении — вызывающий уходит на байтовый путь."""
    from PIL import Image, ImageEnhance  # ленивый импорт: нет Pillow → байтовый путь

    img = Image.open(io.BytesIO(data))
    img.load()
    fmt = (img.format or "JPEG").upper()

    work = img
    # 1) микро-джиттер яркости и контраста — незаметно глазу, меняет пиксели.
    work = ImageEnhance.Brightness(work).enhance(random.uniform(0.985, 1.015))
    work = ImageEnhance.Contrast(work).enhance(random.uniform(0.985, 1.015))

    # 2) обрезка 0–2 px по случайным краям — меняет размер и контент.
    w, h = work.size
    left, top = random.randint(0, 2), random.randint(0, 2)
    right, bottom = random.randint(0, 2), random.randint(0, 2)
    if w - left - right >= 8 and h - top - bottom >= 8 and (left or top or right or bottom):
        work = work.crop((left, top, w - right, h - bottom))

    out = io.BytesIO()
    if fmt in ("JPEG", "JPG", "MPO"):
        if work.mode not in ("RGB", "L"):
            work = work.convert("RGB")
        work.save(out, format="JPEG", quality=random.randint(88, 96), optimize=False)
    elif fmt == "PNG":
        work.save(out, format="PNG")
    elif fmt == "WEBP":
        work.save(out, format="WEBP", quality=random.randint(88, 96))
    elif fmt == "BMP":
        work.save(out, format="BMP")
    else:
        work.save(out, format=fmt)
    return out.getvalue()


def _byte_jitter(data: bytes) -> bytes:
    """Байтовое отличие без переупаковки контента: для mp4/mov — пустой `free`-бокс
    (по стандарту ISO-BMFF, проигрыватели игнорируют), иначе — случайный хвост."""
    payload = os.urandom(random.randint(8, 24))
    if len(data) >= 12 and data[4:8] == b"ftyp":
        box = (8 + len(payload)).to_bytes(4, "big") + b"free" + payload
        return data + box
    return data + payload


def uniquify(data: bytes, *, filename: str = "") -> bytes:
    """Вернуть уникализированный вариант медиа. Никогда не бросает: при любой
    ошибке возвращает исходные байты (отправка важнее уникализации)."""
    try:
        if not data:
            return data
        head = data[:16]
        if _is_still_image(head) and len(data) <= _MAX_IMAGE_BYTES:
            try:
                return _uniquify_image(data)
            except Exception:  # noqa: BLE001 — битое/неоткрываемое → байтовый путь
                log.debug("media_uniquify: пиксельный путь не сработал (%s), байтовый", filename)
                return _byte_jitter(data)
        return _byte_jitter(data)
    except Exception:  # noqa: BLE001 — уникализация не должна срывать отправку
        log.debug("media_uniquify: не удалось (%s), шлём оригинал", filename)
        return data
