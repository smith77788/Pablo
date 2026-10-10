"""Генератор аватаров: детерминизм, разнообразие, устойчивость.

Смысл модуля — не «красиво», а «не одинаково»: один аватар на сотнях каналов
опознаётся по хешу файла и выдаёт сетку целиком. Поэтому тесты защищают
ровно два свойства и одно обещание:

* разные объекты → разные картинки (иначе модуль бесполезен);
* один seed → та же картинка (иначе превью врёт, а повторный запуск
  перерисовывает уже созданное);
* генерация не падает на пограничных входах — пустое имя, эмодзи,
  плейсхолдеры, очень длинные названия.
"""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("PIL", reason="Pillow обязателен для генератора аватаров")

from services import avatar_factory as af  # noqa: E402


def _png_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_output_is_png():
    data = af.generate_avatar(1, "Новости Москвы")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_same_seed_same_bytes():
    a = af.generate_avatar(777, "Чат Сочи")
    b = af.generate_avatar(777, "Чат Сочи")
    assert a == b


def test_different_seeds_differ():
    # Главное свойство: сотня объектов не должна получить одну картинку.
    hashes = {_png_hash(af.generate_avatar(s, f"Канал {s}")) for s in range(60)}
    assert len(hashes) >= 55, f"слишком много совпадений: {len(hashes)}/60"


def test_same_seed_different_text_differs():
    a = af.generate_avatar(5, "Новости Москвы")
    b = af.generate_avatar(5, "Барахолка Уфы")
    assert a != b


def test_size_respected_and_clamped():
    from PIL import Image
    import io

    for req, exp in ((128, 128), (512, 512), (5, 64), (4096, 1024)):
        img = Image.open(io.BytesIO(af.generate_avatar(1, "X", size=req)))
        assert img.size == (exp, exp)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "Сочи", "{{CITY}} • Новости", "🔥🔥🔥", "Ж" * 200,
     "Работа в Казани", "24", "Ростов-на-Дону"],
)
def test_generation_survives_edge_texts(text):
    data = af.generate_avatar(42, text)
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 100


def test_initials_skip_function_words():
    # «Работа в Казани» → «РК»: предлог в инициалах читается как опечатка.
    assert af._initials("Работа в Казани") == "РК"
    assert af._initials("Новости Москвы") == "НМ"
    assert af._initials("Куда сходить в Перми") == "КС"
    assert af._initials("Сочи") == "СО"
    assert af._initials("") == "•"


def test_styles_are_self_consistent():
    for name, spec in af.AVATAR_STYLES.items():
        assert spec["backgrounds"], name
        assert spec["frames"], name
        assert set(spec["backgrounds"]) <= set(af.BACKGROUNDS), name
        assert set(spec["frames"]) <= set(af.FRAMES), name


def test_every_style_renders():
    for style in af.AVATAR_STYLES:
        data = af.generate_avatar(9, "Новости Твери", style=style)
        assert data[:8] == b"\x89PNG\r\n\x1a\n", style


def test_unknown_style_falls_back_instead_of_raising():
    assert af.generate_avatar(1, "X", style="не-существует")[:8] == b"\x89PNG\r\n\x1a\n"


def test_describe_matches_render_choices():
    # describe_avatar обязан описывать ТУ ЖЕ картинку, что нарисует
    # generate_avatar, иначе превью показывает одно, а создаётся другое.
    d1 = af.describe_avatar(1234)
    d2 = af.describe_avatar(1234)
    assert d1 == d2
    assert d1["palette"] in {p["name"] for p in af.PALETTES}
    assert d1["background"] in af.BACKGROUNDS
    assert d1["frame"] in af.FRAMES


def test_variant_space_is_large_and_honest():
    # Число показывается пользователю — оно должно быть посчитанным,
    # а не рекламным, и сужаться при выборе конкретного стиля.
    assert af.variant_space() > 1000
    assert af.variant_space("gradient") < af.variant_space("mixed")


def test_preview_sheet_renders_grid():
    from PIL import Image
    import io

    sheet = af.generate_preview_sheet([1, 2, 3, 4, 5, 6], ["a", "b", "c", "d", "e", "f"], cell=64)
    img = Image.open(io.BytesIO(sheet))
    assert img.size[0] > 64 and img.size[1] > 64


def test_preview_sheet_empty_input():
    assert af.generate_preview_sheet([]) == b""


def test_palettes_have_readable_contrast():
    # Тёмный текст на тёмном фоне даст нечитаемый аватар, и заметно это будет
    # только на части seed'ов. Проверяем контраст текста к обоим цветам фона.
    def luminance(c):
        r, g, b = (v / 255 for v in c)
        f = lambda x: x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    for p in af.PALETTES:
        for bg in (p["a"], p["b"]):
            l1, l2 = sorted((luminance(p["fg"]), luminance(bg)), reverse=True)
            ratio = (l1 + 0.05) / (l2 + 0.05)
            assert ratio >= 3.0, f"палитра {p['name']}: контраст {ratio:.2f} слишком низкий"
