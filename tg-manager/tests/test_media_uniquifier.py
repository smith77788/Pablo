"""Регресс-тесты уникализации медиа (services/media_uniquifier).

Проверяем: пиксельный путь для растровых изображений (вариант отличается,
но остаётся валидным изображением того же формата и близкого размера), байтовый
путь для видео/прочего, и что функция НИКОГДА не бросает.
"""

import io

import pytest

from services import media_uniquifier as mu

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _jpeg(w=64, h=48, color=(120, 40, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _png(w=64, h=48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (10, 220, 130, 255)).save(buf, format="PNG")
    return buf.getvalue()


# ── Пиксельный путь: JPEG ────────────────────────────────────────────────────
def test_jpeg_uniquified_differs_but_valid():
    src = _jpeg()
    out = mu.uniquify(src, filename="pic.jpg")
    assert out != src                       # байты отличаются
    img = Image.open(io.BytesIO(out))       # остаётся валидным JPEG
    assert img.format == "JPEG"
    w, h = img.size
    assert 60 <= w <= 64 and 44 <= h <= 48  # обрезка не более 2px по каждому краю


def test_two_calls_give_different_bytes():
    src = _jpeg()
    a = mu.uniquify(src)
    b = mu.uniquify(src)
    assert a != b                           # каждая копия уникальна


# ── Пиксельный путь: PNG сохраняет формат ────────────────────────────────────
def test_png_stays_png():
    src = _png()
    out = mu.uniquify(src, filename="pic.png")
    assert out != src
    assert Image.open(io.BytesIO(out)).format == "PNG"


# ── Байтовый путь: mp4 получает free-бокс ────────────────────────────────────
def test_mp4_gets_free_box():
    # минимальный ISO-BMFF заголовок: size + 'ftyp' + brand
    src = (0x18).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 16
    out = mu.uniquify(src, filename="clip.mp4")
    assert out.startswith(src)              # исходные байты не тронуты
    assert b"free" in out[len(src):]        # добавлен free-бокс
    assert len(out) > len(src)


def test_unknown_binary_gets_tail():
    src = b"\x00\x01\x02\x03not-a-known-format" * 4
    out = mu.uniquify(src)
    assert out != src
    assert out.startswith(src)              # хвост добавлен, начало сохранено


# ── Никогда не бросает ───────────────────────────────────────────────────────
def test_empty_returns_empty():
    assert mu.uniquify(b"") == b""


def test_corrupt_jpeg_falls_back_no_raise():
    # валидная JPEG-сигнатура, но битое тело → пиксельный путь падает → байтовый
    src = b"\xff\xd8\xff" + b"\x00" * 200
    out = mu.uniquify(src, filename="broken.jpg")
    assert out != src                       # что-то вернулось (байтовый джиттер)
    assert out.startswith(src)
