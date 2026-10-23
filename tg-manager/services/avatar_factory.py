"""Генератор аватаров: одна картинка на объект, не одна на весь проект.

Зачем не «загрузить один файл и поставить всем»: одинаковый аватар на сотнях
каналов — самый дешёвый признак сетки. Его видит и человек, и любой поиск по
хешу изображения. Здесь каждый объект получает СВОЮ картинку, но в едином
визуальном языке проекта: узнаваемо как бренд, не идентично как копия.

Вариативность набирается композиционно (палитра × фон × рамка × шрифт ×
кегль × позиция × глиф), поэтому пространство комбинаций считается тысячами
даже без внешних ассетов — см. `variant_space()`.

Детерминизм: `seed` полностью определяет картинку. Один и тот же объект в
превью и в исполнении получит побайтово одинаковый аватар, а повторный запуск
не перерисует уже созданное.

Зависимости: только Pillow. Шрифт ищется среди системных; если TTF нет вовсе,
модуль честно сообщает об этом (`fonts_available()`), а не молча рисует
нечитаемый растровый примитив.
"""

from __future__ import annotations

import io
import logging
import math
import os
import random

log = logging.getLogger(__name__)

AVATAR_SIZE = 512

# ── Палитры ─────────────────────────────────────────────────────────────────
# Пары (фон-1, фон-2) + цвет текста. Подобраны так, чтобы контраст текста был
# достаточным на любом из фонов — иначе часть сгенерированных аватаров вышла
# бы нечитаемой, а проверить их все вручную невозможно.
PALETTES: list[dict] = [
    {"name": "indigo", "a": (49, 46, 129), "b": (79, 70, 229), "fg": (255, 255, 255)},
    {"name": "ocean", "a": (12, 74, 110), "b": (12, 151, 214), "fg": (255, 255, 255)},
    {"name": "teal", "a": (19, 78, 74), "b": (17, 161, 145), "fg": (255, 255, 255)},
    {"name": "forest", "a": (20, 83, 45), "b": (24, 159, 74), "fg": (255, 255, 255)},
    {"name": "amber", "a": (120, 53, 15), "b": (198, 126, 6), "fg": (255, 255, 255)},
    {"name": "sunset", "a": (154, 52, 18), "b": (229, 105, 20), "fg": (255, 255, 255)},
    {"name": "crimson", "a": (127, 29, 29), "b": (239, 68, 68), "fg": (255, 255, 255)},
    {"name": "rose", "a": (131, 24, 67), "b": (244, 63, 94), "fg": (255, 255, 255)},
    {"name": "violet", "a": (76, 29, 149), "b": (139, 92, 246), "fg": (255, 255, 255)},
    {"name": "plum", "a": (88, 28, 135), "b": (192, 38, 211), "fg": (255, 255, 255)},
    {"name": "slate", "a": (30, 41, 59), "b": (100, 116, 139), "fg": (255, 255, 255)},
    {"name": "graphite", "a": (24, 24, 27), "b": (82, 82, 91), "fg": (255, 255, 255)},
    {"name": "steel", "a": (30, 58, 138), "b": (84, 144, 220), "fg": (255, 255, 255)},
    {"name": "moss", "a": (86, 124, 41), "b": (132, 204, 22), "fg": (26, 32, 12)},
    {"name": "sand", "a": (168, 162, 158), "b": (231, 229, 228), "fg": (41, 37, 36)},
    {"name": "mint", "a": (6, 95, 70), "b": (39, 162, 117), "fg": (255, 255, 255)},
    {"name": "sky", "a": (7, 89, 133), "b": (44, 152, 200), "fg": (255, 255, 255)},
    {"name": "wine", "a": (69, 10, 10), "b": (185, 28, 28), "fg": (255, 255, 255)},
    {"name": "cobalt", "a": (23, 37, 84), "b": (59, 130, 246), "fg": (255, 255, 255)},
    {"name": "olive", "a": (66, 32, 6), "b": (161, 98, 7), "fg": (255, 255, 255)},
    {"name": "ice", "a": (203, 213, 225), "b": (241, 245, 249), "fg": (30, 41, 59)},
    {"name": "coral", "a": (136, 19, 55), "b": (220, 98, 116), "fg": (255, 255, 255)},
]

BACKGROUNDS: tuple[str, ...] = (
    "linear",     # линейный градиент
    "diagonal",   # диагональный градиент
    "radial",     # радиальный градиент из смещённого центра
    "split",      # диагональный раздел двух цветов
    "arc",        # угловая дуга поверх заливки
    "rings",      # концентрические кольца
    "dots",       # регулярная точечная сетка
    "bars",       # диагональные полосы
    "solid",      # плоская заливка
)

FRAMES: tuple[str, ...] = ("none", "ring", "inner", "corner", "double")

TEXT_POSITIONS: tuple[str, ...] = ("center", "upper", "lower")

# Кандидаты шрифтов: сначала гротески с кириллицей, потом что найдётся.
_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
)

# Стиль = сужение пространства вариаций. Пользователь выбирает «как это должно
# выглядеть», а не 7 отдельных параметров, но внутри стиля объекты всё равно
# различаются.
AVATAR_STYLES: dict[str, dict] = {
    "gradient": {
        "label": "🎨 Градиент",
        "backgrounds": ("linear", "diagonal", "radial"),
        "frames": ("none", "ring", "inner"),
    },
    "flat": {
        "label": "⬛ Плоский",
        "backgrounds": ("solid", "split"),
        "frames": ("none", "inner", "double"),
    },
    "geometric": {
        "label": "◼️ Геометрия",
        "backgrounds": ("split", "arc", "bars", "rings"),
        "frames": ("none", "corner", "ring"),
    },
    "pattern": {
        "label": "⚙️ Паттерн",
        "backgrounds": ("dots", "bars", "rings"),
        "frames": ("none", "inner", "ring"),
    },
    "mixed": {
        "label": "🌈 Смешанный",
        "backgrounds": BACKGROUNDS,
        "frames": FRAMES,
    },
}

DEFAULT_STYLE = "mixed"


def _available_fonts() -> list[str]:
    return [p for p in _FONT_CANDIDATES if os.path.exists(p)]


def fonts_available() -> bool:
    """Есть ли в системе хоть один TTF. False → аватары будут нечитаемыми."""
    return bool(_available_fonts())


def variant_space(style: str | None = None) -> int:
    """Честная оценка числа комбинаций стиля (без учёта самого текста).

    Используется в UI, чтобы обещание «тысячи вариантов» было посчитанным,
    а не рекламным.
    """
    spec = AVATAR_STYLES.get(style or DEFAULT_STYLE, AVATAR_STYLES[DEFAULT_STYLE])
    fonts = max(1, len(_available_fonts()))
    return (
        len(PALETTES)
        * len(spec["backgrounds"])
        * len(spec["frames"])
        * len(TEXT_POSITIONS)
        * fonts
        * 4  # ступени кегля
        * 2  # прямой / инвертированный фон
    )


# Служебные слова не несут смысла в инициалах: «Работа в Казани» должно дать
# «РК», а не «РВ» — предлог в аватаре выглядит как опечатка.
_STOP_WORDS = frozenset(
    {
        "в", "во", "на", "и", "по", "для", "за", "у", "из", "о", "об", "с", "со",
        "к", "от", "до", "при", "про", "the", "of", "in", "at", "and", "for", "a",
    }
)


def _initials(text: str) -> str:
    """1–2 буквы для глифа: «Новости Москвы» → «НМ», «Работа в Казани» → «РК»."""
    raw = [w.strip("•—-–|:,.()[]") for w in (text or "").replace("•", " ").split()]
    words = [w for w in raw if w and w[0].isalnum() and w.lower() not in _STOP_WORDS]
    if not words:
        return "•"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


def _mix(c1, c2, t: float):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def _shade(color, factor: float):
    """factor<1 — темнее, >1 — светлее. Границы обрезаются."""
    return tuple(max(0, min(255, int(round(c * factor)))) for c in color)


def _draw_background(img, kind: str, a, b, rng) -> None:
    from PIL import Image, ImageDraw

    size = img.size[0]
    draw = ImageDraw.Draw(img)

    if kind == "solid":
        draw.rectangle([0, 0, size, size], fill=b)
        return

    if kind in ("linear", "diagonal", "radial"):
        # Градиент строится в маленьком буфере и растягивается: рисовать 512
        # линий по пикселю дорого и незаметно лучше.
        small = 64
        grad = Image.new("RGB", (small, small))
        gp = grad.load()
        for y in range(small):
            for x in range(small):
                if kind == "linear":
                    t = y / (small - 1)
                elif kind == "diagonal":
                    t = (x + y) / (2 * (small - 1))
                else:  # radial
                    cx, cy = small * 0.35, small * 0.3
                    d = math.hypot(x - cx, y - cy)
                    t = min(1.0, d / (small * 1.05))
                gp[x, y] = _mix(a, b, t)
        img.paste(grad.resize(img.size, Image.BILINEAR), (0, 0))
        return

    # Остальные фоны рисуются поверх плоской заливки.
    draw.rectangle([0, 0, size, size], fill=a)

    if kind == "split":
        offset = rng.uniform(0.35, 0.65)
        draw.polygon(
            [(0, size * offset), (size, size * (offset - 0.25)), (size, size), (0, size)],
            fill=b,
        )
    elif kind == "arc":
        r = size * rng.uniform(0.75, 1.15)
        cx, cy = size * rng.uniform(0.6, 1.0), size * rng.uniform(0.6, 1.0)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=b)
    elif kind == "rings":
        rings = rng.randint(3, 6)
        for i in range(rings, 0, -1):
            r = size * (0.18 + 0.14 * i)
            col = _mix(a, b, i / (rings + 1))
            draw.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], outline=col,
                         width=max(2, size // 48))
    elif kind == "dots":
        step = size // rng.randint(7, 12)
        r = max(2, step // 6)
        for y in range(step // 2, size, step):
            for x in range(step // 2, size, step):
                draw.ellipse([x - r, y - r, x + r, y + r], fill=b)
    elif kind == "bars":
        step = size // rng.randint(6, 11)
        w = max(3, step // 3)
        for i in range(-size, size * 2, step):
            draw.line([(i, 0), (i + size, size)], fill=b, width=w)


def _draw_frame(img, kind: str, color, rng) -> None:
    from PIL import ImageDraw

    size = img.size[0]
    draw = ImageDraw.Draw(img)
    if kind == "ring":
        w = max(4, size // 40)
        pad = w
        draw.ellipse([pad, pad, size - pad, size - pad], outline=color, width=w)
    elif kind == "inner":
        w = max(3, size // 64)
        pad = size // 12
        draw.rectangle([pad, pad, size - pad, size - pad], outline=color, width=w)
    elif kind == "double":
        w = max(2, size // 96)
        for pad in (size // 14, size // 8):
            draw.rectangle([pad, pad, size - pad, size - pad], outline=color, width=w)
    elif kind == "corner":
        w = max(4, size // 40)
        pad = size // 10
        ln = size // 4
        draw.line([(pad, pad), (pad + ln, pad)], fill=color, width=w)
        draw.line([(pad, pad), (pad, pad + ln)], fill=color, width=w)
        draw.line([(size - pad, size - pad), (size - pad - ln, size - pad)], fill=color, width=w)
        draw.line([(size - pad, size - pad), (size - pad, size - pad - ln)], fill=color, width=w)


def _load_font(path: str | None, px: int):
    from PIL import ImageFont

    if path:
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            log.debug("avatar_factory: не удалось загрузить шрифт %s", path, exc_info=True)
    return ImageFont.load_default()


def describe_avatar(seed: int, *, style: str | None = None) -> dict:
    """Параметры аватара без его отрисовки — для превью и отладки."""
    rng = random.Random(seed)
    spec = AVATAR_STYLES.get(style or DEFAULT_STYLE, AVATAR_STYLES[DEFAULT_STYLE])
    palette = rng.choice(PALETTES)
    background = rng.choice(spec["backgrounds"])
    frame = rng.choice(spec["frames"])
    position = rng.choice(TEXT_POSITIONS)
    fonts = _available_fonts()
    font_path = rng.choice(fonts) if fonts else None
    inverted = rng.random() < 0.5
    scale = rng.choice((0.34, 0.40, 0.46, 0.52))
    return {
        "palette": palette["name"],
        "background": background,
        "frame": frame,
        "position": position,
        "font": os.path.basename(font_path) if font_path else "default",
        "inverted": inverted,
        "scale": scale,
    }


def generate_avatar(
    seed: int,
    text: str = "",
    *,
    style: str | None = None,
    size: int = AVATAR_SIZE,
) -> bytes:
    """Отрисовать PNG-аватар. Один seed → всегда одна и та же картинка.

    `text` — название объекта; из него берутся инициалы. Пустой текст даёт
    чисто графический аватар (это допустимо, а не ошибка).
    """
    from PIL import Image, ImageDraw

    size = max(64, min(1024, int(size)))
    rng = random.Random(seed)
    spec = AVATAR_STYLES.get(style or DEFAULT_STYLE, AVATAR_STYLES[DEFAULT_STYLE])

    palette = rng.choice(PALETTES)
    background = rng.choice(spec["backgrounds"])
    frame = rng.choice(spec["frames"])
    position = rng.choice(TEXT_POSITIONS)
    fonts = _available_fonts()
    font_path = rng.choice(fonts) if fonts else None
    inverted = rng.random() < 0.5
    scale = rng.choice((0.34, 0.40, 0.46, 0.52))

    a, b = palette["a"], palette["b"]
    if inverted:
        a, b = b, a
    fg = palette["fg"]

    img = Image.new("RGB", (size, size), a)
    _draw_background(img, background, a, b, rng)

    frame_color = _shade(fg, 0.92) if frame != "none" else fg
    _draw_frame(img, frame, frame_color, rng)

    glyph = _initials(text)
    if glyph:
        draw = ImageDraw.Draw(img)
        # Рамка съедает поля: без учёта safe-area текст в позициях upper/lower
        # налезает на неё, и заметно это лишь на части seed'ов — вручную такое
        # не отловишь, поэтому область считается из типа рамки.
        safe_ratio = 0.62 if frame in ("inner", "double", "corner") else 0.74
        safe_top = size * (0.16 if frame in ("inner", "double", "corner") else 0.10)
        safe_bottom = size - safe_top

        px = int(size * scale)
        font = _load_font(font_path, px)
        # Кегль подгоняется под ширину: длинные инициалы («ЩЖ») иначе выходят
        # за края, и это видно только на части сгенерированных аватаров.
        for _ in range(8):
            bbox = draw.textbbox((0, 0), glyph, font=font)
            if bbox[2] - bbox[0] <= size * safe_ratio or px <= 12:
                break
            px = int(px * 0.88)
            font = _load_font(font_path, px)

        bbox = draw.textbbox((0, 0), glyph, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx = (size - tw) / 2 - bbox[0]
        if position == "center":
            top = (size - th) / 2
        elif position == "upper":
            top = size * 0.32 - th / 2
        else:
            top = size * 0.66 - th / 2
        top = max(safe_top, min(top, safe_bottom - th))
        cy = top - bbox[1]

        # Мягкая тень: без неё светлый текст на светлом участке градиента
        # теряется, а участок зависит от seed — вручную не проверишь.
        shadow = _shade(a, 0.45)
        off = max(1, size // 180)
        draw.text((cx + off, cy + off), glyph, font=font, fill=shadow)
        draw.text((cx, cy), glyph, font=font, fill=fg)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def generate_preview_sheet(seeds, texts=None, *, style: str | None = None, cell: int = 128) -> bytes:
    """Контактный лист из нескольких аватаров — для предпросмотра плана.

    Пользователь должен увидеть РАЗНООБРАЗИЕ до запуска, а не один пример:
    именно однообразие — то, ради чего этот модуль существует.
    """
    from PIL import Image

    seeds = list(seeds)[:12]
    if not seeds:
        return b""
    texts = list(texts or [])
    cols = min(4, len(seeds))
    rows = math.ceil(len(seeds) / cols)
    gap = max(4, cell // 24)
    sheet = Image.new(
        "RGB",
        (cols * cell + (cols + 1) * gap, rows * cell + (rows + 1) * gap),
        (17, 17, 20),
    )
    for i, seed in enumerate(seeds):
        text = texts[i] if i < len(texts) else ""
        tile = Image.open(io.BytesIO(generate_avatar(seed, text, style=style, size=cell)))
        x = gap + (i % cols) * (cell + gap)
        y = gap + (i // cols) * (cell + gap)
        sheet.paste(tile, (x, y))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
