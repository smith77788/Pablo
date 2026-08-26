"""Каждая функция из onclick мини-аппа обязана быть определена.

ЗАЧЕМ. Кнопка, зовущая несуществующую функцию, не «ломается» заметно: браузер
кидает ReferenceError в консоль, которой пользователь не видит, и клик просто не
делает НИЧЕГО. Снаружи это неотличимо от «приложение зависло» — самая частая
формулировка жалобы.

Уже существующий tests/test_ranking_screen_wired проверяет то же самое для
ОДНОГО экрана; здесь — для всех сразу, поэтому следующий такой экран не проедет.

ВАЖНО ПРО ОБЛАСТЬ ПОИСКА. Часть экранов вынесена в mini_app/screens/*.js и
подключается <script src> после основного скрипта. Проверка, которая смотрит
только в index.html, объявит эти функции несуществующими и «найдёт» полтора
десятка мёртвых кнопок, которых нет. Поэтому имена собираются из ВСЕХ файлов
мини-аппа, а тест ниже следит, чтобы файлы экранов не выпали из области поиска.
"""
from __future__ import annotations

import collections
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MINI_APP = os.path.join(ROOT, "mini_app")
_HTML = os.path.join(_MINI_APP, "index.html")
_SCREENS = os.path.join(_MINI_APP, "screens")

_INLINE = re.compile(
    r'\bon(?:click|change|input|submit|keyup|keydown|focus|blur)\s*=\s*"([^"]*)"')
# Вызов ИМЕНИ, а не метода: `foo(` считаем, `obj.foo(` — нет.
_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")

_KEYWORDS = {"if", "for", "while", "return", "typeof", "new", "function",
             "catch", "switch", "await", "delete", "void", "in", "of"}
# Глобальные объекты браузера: определять их нам не нужно.
_BUILTINS = {"alert", "confirm", "prompt", "parseInt", "parseFloat", "Number",
             "String", "Boolean", "Array", "Object", "JSON", "Math", "Date",
             "setTimeout", "setInterval", "clearTimeout", "clearInterval",
             "encodeURIComponent", "decodeURIComponent", "fetch", "Promise",
             "isNaN", "RegExp", "Set", "Map", "URLSearchParams", "Blob"}


def _html() -> str:
    """Разметка мини-аппа — в ней живут inline-обработчики."""
    return open(_HTML, encoding="utf-8").read()


def _sources() -> list[str]:
    """Весь код мини-аппа: index.html + вынесенные экраны screens/*.js."""
    out = [_html()]
    if os.path.isdir(_SCREENS):
        for fname in sorted(os.listdir(_SCREENS)):
            if fname.endswith(".js"):
                out.append(open(os.path.join(_SCREENS, fname),
                                encoding="utf-8").read())
    return out


def defined_names(html: str) -> set[str]:
    """Имена, доступные из inline-обработчика (то есть глобальные)."""
    out: set[str] = set()
    out |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", html))
    out |= set(re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b", html))
    out |= set(re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", html))
    out |= set(re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[A-Za-z_$][\w$]*\s*=>", html))
    out |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", html))
    return out


def inline_calls(html: str) -> dict[str, list[int]]:
    """{имя: [строки]} — что зовут обработчики разметки."""
    out: dict[str, list[int]] = collections.defaultdict(list)
    for m in _INLINE.finditer(html):
        line = html[:m.start()].count("\n") + 1
        for c in _CALL.finditer(m.group(1)):
            name = c.group(1)
            if name in _KEYWORDS:
                continue
            if len(out[name]) < 3:
                out[name].append(line)
    return out


def all_defined() -> set[str]:
    known: set[str] = set()
    for src in _sources():
        known |= defined_names(src)
    return known


def undefined() -> dict[str, list[int]]:
    known = all_defined() | _BUILTINS
    return {n: lines for n, lines in inline_calls(_html()).items()
            if n not in known}


# ── Тесты ─────────────────────────────────────────────────────────────────────

def test_every_inline_handler_is_defined():
    bad = undefined()
    assert not bad, (
        "разметка зовёт функции, которых нет:\n"
        + "\n".join(f"  {n} — index.html:{', '.join(map(str, lines))}"
                    for n, lines in sorted(bad.items()))
        + "\n\nКлик по такой кнопке не делает ничего: ошибка уходит в консоль, "
          "которой пользователь не видит."
    )


def test_scanner_sees_both_sides():
    """Пустые множества сделали бы тест зелёным навсегда."""
    assert len(all_defined()) > 300, "функции мини-аппа не разобрались"
    assert len(inline_calls(_html())) > 200, "обработчики разметки не разобрались"


def test_scanner_covers_extracted_screen_files():
    """Файлы экранов обязаны входить в область поиска.

    Без них проверка объявит вынесенные экраны мёртвыми и будет «находить»
    кнопки, которые прекрасно работают, — а детектор с ложными срабатываниями
    обесценивает сам себя: его начинают обходить.
    """
    assert os.path.isdir(_SCREENS), "каталог вынесенных экранов исчез"
    assert len(_sources()) > 1, "screens/*.js не попали в область поиска"
    known = all_defined()
    for fn in ("openSpintax", "submitSpin", "rerollSpin", "openUnifiedDashboard"):
        assert fn in known, (
            f"{fn} определена в screens/*.js, но сканер её не видит — "
            "проверка начнёт врать про мёртвые кнопки")


def test_scanner_ignores_method_calls():
    """`event.stopPropagation()` — метод объекта, а не глобальная функция."""
    calls = inline_calls(
        '<div onclick="event.stopPropagation(); doThing(); a.b.c(1)"></div>')
    assert set(calls) == {"doThing"}, calls


def test_scanner_finds_a_planted_dead_button():
    """Детектор обязан ловить именно то, ради чего написан."""
    html = "<div onclick=\"openNowhere()\"></div>\nfunction other() {}\n"
    known = defined_names(html) | _BUILTINS
    missing = [n for n in inline_calls(html) if n not in known]
    assert missing == ["openNowhere"]


def test_extracted_screens_are_actually_loaded():
    """Вынесенный экран обязан подключаться тегом <script src>.

    Файл, который никто не грузит, — те же мёртвые кнопки, только незаметнее:
    имена определены в репозитории и не существуют в браузере.
    """
    html = _html()
    if not os.path.isdir(_SCREENS):
        return
    for fname in sorted(os.listdir(_SCREENS)):
        if fname.endswith(".js"):
            assert f'screens/{fname}"' in html, (
                f"screens/{fname} не подключён в index.html — его функции "
                "не существуют в браузере")
