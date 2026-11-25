"""Экран, который показал часть списка, даёт дверь к остальному.

РАЗРЫВ. Сервер научился отдавать страницу с общим числом, но дверь «Загрузить
ещё» на экране от этого не появляется сама: пока её нет, усечение выглядит как
«больше ничего нет». У трёх экранов это читалось особенно плохо:

* прогрев печатал «Всего планов N» ИЗ ОТВЕТА над списком из пятидесяти строк,
  то есть сам себе противоречил;
* подпись раздела постов нарратив-кампании считала посты по длине отданного
  куска — неверное число, выданное как факт;
* радар ниши молчал вовсе.

Проверяем ровно это: подпись числа берётся из конверта страницы, а не из длины
куска, и у списка есть вызов общей двери `UI.more` со своей функцией догрузки,
которая на экране определена.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
NICHE = (ROOT / "mini_app" / "screens" / "niche.js").read_text(encoding="utf-8")

# экран → (где искать, имя функции догрузки)
_DOORS = (
    ("прогрев флота", HTML, "warmupMore"),
    ("посты нарратив-кампании", HTML, "narrPostsMore"),
    ("радар ниши", NICHE, "nrMore"),
)


def test_every_paged_screen_calls_the_one_door():
    for name, src, fn in _DOORS:
        assert f"fn: '{fn}'" in src, f"{name}: список не зовёт общую дверь UI.more"
        assert re.search(r"function\s+" + fn + r"\s*\(", src), (
            f"{name}: кнопка догрузки ведёт в несуществующую функцию {fn}")


def test_the_door_asks_for_the_next_page_and_not_for_the_first():
    """Догрузка обязана спрашивать offset от уже показанного, иначе она зацикливается."""
    for src, call in ((HTML, "/api/miniapp/warmup?limit=50&offset="),
                      (HTML, "?limit=50&offset="),
                      (NICHE, "/api/miniapp/niche/overview?limit=20&offset=")):
        assert call in src, f"не нашли запрос следующей страницы: {call}"
    assert "offset=' + _WU_PLANS.length" in HTML, (
        "прогрев просит следующую страницу не от показанного — догрузка вернёт то же")
    assert "offset=` + _NARRD_POSTS.length" in HTML, (
        "посты кампании просят следующую страницу не от показанного")
    assert "offset=' + have" in NICHE, (
        "радар ниши просит следующую страницу не от показанного")


def test_the_posts_counter_comes_from_the_envelope():
    m = re.search(r"Посты \(\$\{([^}]+)\}\)", HTML)
    assert m, "не нашли подпись раздела постов нарратив-кампании"
    assert "_NARRD_PAGE" in m.group(1), (
        "число постов снова считается по длине отданного куска: у кампании на три "
        "месяца это неверное число, выданное как факт")


def _window(src: str, fn: str, size: int = 900) -> str:
    """Окно кода от объявления функции: тело может быть и в одну строку."""
    m = re.search(r"function\s+" + fn + r"\s*\(", src)
    assert m, fn
    return src[m.start():m.start() + size]


def test_a_failed_load_more_does_not_wipe_what_is_already_shown():
    """Сбой догрузки — это тост, а не пустой экран вместо показанных строк."""
    for name, src, fn in _DOORS:
        body = _window(src, fn)
        if "toast(" in body:
            continue
        # Догрузка бывает тонкой обёрткой — тогда спрашиваем того, кого она зовёт.
        hop = re.search(r"\{\s*(?:return\s+)?(\w+)\(", body)
        assert hop, f"{name}: у догрузки нет ни обработки сбоя, ни вызова загрузчика"
        assert "toast(" in _window(src, hop.group(1), 1600), (
            f"{name}: сбой догрузки стирает уже показанное вместо того, чтобы "
            f"сказать о себе тостом")
