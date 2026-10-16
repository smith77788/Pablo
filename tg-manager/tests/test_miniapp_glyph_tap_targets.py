"""Зона нажатия у значков-действий в мини-аппе.

Замер в браузере на экране шириной 360 CSS-пикселей (самый узкий реальный
телефон) показал десятки кликабельных значков высотой 14–22 пикселя: «🗑» в
списке сохранённых сегментов — 14×20, четыре значка подряд в правиле намерений
— по 19×21, стрелка «›» в списке нод — 5 пикселей шириной. Палец такого
размера не различает: промах по «🗑» удаляет не тот объект, а отменить удаление
в мини-аппе нечем.

Тест держит результат: у каждого кликабельного значка (элемент с onclick, чьё
видимое содержимое — один-два символа) должна быть объявленная высота не меньше
30 пикселей — своя, через класс или inline. Высота считается по таблице стилей
самого мини-аппа, а не по отдельному списку «правильных» классов: класс,
который перестал быть большим, тест поймает так же, как снятый класс.
"""
from __future__ import annotations

import re

import pytest

from tests.miniapp_source import miniapp_html, screen_files

MIN_TAP_PX = 30
#: Классы-зоны нажатия, добавленные специально ради этого: если кто-то
#: уменьшит их, все значки разом снова станут мелкими, а по каждому отдельному
#: месту тест уже не сработает — там больше нет inline-размера.
HIT_CLASSES = {"icon-tap": 36, "link-tap": 34, "icon-btn": 40}

#: Содержимое из одного-трёх «символов» (эмодзи, стрелка, ${выражение}) —
#: то, у чего нет длинного текста, за который можно зацепиться пальцем.
_GLYPH = re.compile(r"^(?:\s*(?:\$\{[^}]*\}|[^\w\s<>&]|&#?\w+;)\s*){1,3}$")
_CLICKABLE = re.compile(r"<(span|div|a)\b([^>]*\bonclick=[^>]*)>([^<]{0,14})</\1>")


def _css() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", miniapp_html(), re.S))


def _declared_height(decls: str) -> int:
    """Высота, которую объявляет блок стилей: явная либо из паддинга и шрифта."""
    m = re.search(r"(?:^|;)\s*(?:min-)?height\s*:\s*(\d+)px", decls)
    if m:
        return int(m.group(1))
    pad = 0
    p = re.search(r"(?:^|;)\s*padding\s*:\s*([^;]+)", decls)
    if p:
        px = [v for v in p.group(1).split() if v.endswith("px")]
        if px:
            pad = int(px[0][:-2])
    else:
        p = re.search(r"(?:^|;)\s*padding-top\s*:\s*(\d+)px", decls)
        if p:
            pad = int(p.group(1))
    fs = re.search(r"(?:^|;)\s*font-size\s*:\s*(\d+)px", decls)
    return 2 * pad + int((int(fs.group(1)) if fs else 16) * 1.35)


def _class_heights() -> dict[str, int]:
    out: dict[str, int] = {}
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css()):
        h = _declared_height(body)
        for cls in re.findall(r"\.([A-Za-z][\w-]*)", sel):
            out[cls] = max(out.get(cls, 0), h)
    return out


def _sources() -> dict[str, str]:
    src = {"mini_app/index.html": miniapp_html()}
    for p in screen_files():
        src[f"mini_app/screens/{p.name}"] = p.read_text(encoding="utf-8")
    return src


def _glyph_controls() -> list[tuple[str, int, str, str]]:
    """Все кликабельные значки мини-аппа: файл, строка, содержимое, атрибуты."""
    found = []
    for name, text in _sources().items():
        for m in _CLICKABLE.finditer(text):
            attrs, inner = m.group(2), m.group(3)
            if not inner.strip() or not _GLYPH.match(inner):
                continue
            found.append((name, text[: m.start()].count("\n") + 1, inner.strip(), attrs))
    return found


def _height_of(attrs: str, heights: dict[str, int]) -> int:
    cls = re.search(r'class="([^"]*)"', attrs)
    names = re.sub(r"\$\{[^}]*\}", " ", cls.group(1)).split() if cls else []
    h = max([heights.get(n, 0) for n in names] or [0])
    inline = re.search(r"min-height\s*:\s*(\d+)px", attrs)
    return max(h, int(inline.group(1))) if inline else h


def test_detector_sees_the_controls():
    """Пустой список находок ничего не доказывает — сначала убеждаемся, что
    измеритель вообще видит значки мини-аппа."""
    controls = _glyph_controls()
    assert len(controls) >= 40, f"кликабельных значков найдено всего {len(controls)} — разбор разметки сломался"


def test_stylesheet_heights_are_read_correctly():
    """Измеритель проверяется на заведомо известных правилах."""
    heights = _class_heights()
    assert heights.get("icon-btn", 0) >= 40, "высота .icon-btn (40px) не прочиталась"
    assert heights.get("back", 0) >= 40, "высота .back (40px) не прочиталась"
    assert _declared_height("font-size:11px;color:var(--accent);cursor:pointer") < MIN_TAP_PX, \
        "блок без паддинга и высоты не должен считаться крупным"


@pytest.mark.parametrize("cls,least", sorted(HIT_CLASSES.items()))
def test_hit_area_classes_stay_big(cls, least):
    h = _class_heights().get(cls, 0)
    assert h >= least, f".{cls} объявлен на {h}px вместо {least}px — все значки с этим классом стали мелкими"


def test_every_glyph_control_is_reachable_by_finger():
    heights = _class_heights()
    small = [
        f"{name}:{line} «{inner}» — {_height_of(attrs, heights)}px"
        for name, line, inner, attrs in _glyph_controls()
        if _height_of(attrs, heights) < MIN_TAP_PX
    ]
    assert not small, (
        "кликабельный значок меньше "
        f"{MIN_TAP_PX}px по высоте — пальцем по нему не попасть:\n  " + "\n  ".join(small)
    )
