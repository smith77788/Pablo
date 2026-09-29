"""Аналитика: строки ведут дальше, а потолки выдачи признаются.

Экран показывал три раздела подряд и в двух из них честно помечал строки
`cursor:default` — то есть сам сообщал, что это тупик. Топ ботов не открывал
бота, слово из поиска не открывало позиции, а пустое состояние говорило «Нет
данных» и не давало сделать ничего.

Отдельная ложь — потолки: сервер отдаёт пять ботов и последние десять слов,
а подпись рядом гласила «N ключей отслеживается», показывая 10 при сотне.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"конец функции {name} не найден")


def _py_func(name: str) -> str:
    with open(API, encoding="utf-8") as f:
        src = f.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


def test_screen_still_has_its_three_sections():
    """Антивакуумность."""
    h = _html()
    for sec in ("Рост подписчиков", "Топ ботов", "Позиции в поиске"):
        assert sec in h, f"раздел «{sec}» пропал"


def test_no_row_declares_itself_a_dead_end():
    body = _js_func("loadAnalytics")
    assert 'style="cursor:default"' not in body, "строка сама помечена как нерабочая"


def test_bot_row_opens_the_bot():
    body = _js_func("loadAnalytics")
    assert "openBot(" in body, "топ ботов не открывает бота"


def test_keyword_row_opens_positions():
    body = _js_func("loadAnalytics")
    assert "openRanking()" in body, "слово не открывает экран позиций"


def test_sections_have_a_way_to_the_full_list():
    h = _html()
    assert "все боты" in h and "все слова" in h, "из разделов нет выхода к полным спискам"


def test_empty_states_offer_the_next_step():
    body = _js_func("loadAnalytics")
    # Комментарии не в счёт — смотрим только то, что попадает на экран.
    visible = "\n".join(l for l in body.split("\n") if not l.strip().startswith("//"))
    assert "Нет данных" not in visible, "«Нет данных» ничего не говорит и никуда не ведёт"
    assert "Ботов пока нет" in body and "Слова не отслеживаются" in body
    assert "goTab('bots')" in body


def test_empty_growth_says_which_emptiness_it_is():
    """«Ботов нет» и «за неделю никто не пришёл» — разные новости."""
    body = _js_func("loadAnalytics")
    assert "считать некого" in body
    assert "новых подписчиков не было" in body


def test_caps_are_admitted():
    body = _js_func("loadAnalytics")
    assert "anCap(" in body, "потолки выдачи по-прежнему не признаются"
    cap = _js_func("anCap")
    assert "Показаны" in cap and "из" in cap
    api = _py_func("analytics")
    assert "keywords_total" in api and "bots_total" in api, "сервер не отдаёт полные счётчики"


def test_keyword_counter_is_not_the_page_size():
    """Подпись «N ключей отслеживается» показывала размер страницы, не всё."""
    body = _js_func("loadAnalytics")
    assert "d.keywords_total" in body
    assert "kws.length+' ключей отслеживается'" not in body


def test_unmeasured_keyword_is_not_called_missing_data():
    body = _js_func("loadAnalytics")
    assert "ещё не мерили" in body
