"""Кнопка, чей обработчик нигде не определён, — мёртвая кнопка.

ЗАЧЕМ ОТДЕЛЬНО ОТ test_every_screen_has_a_way_in. Тот файл стережёт обратное
направление: функция есть, а ссылок на неё нет — мёртвый код. Здесь наоборот:
ссылка есть, а функции нет. Владелец жмёт, ничего не происходит, в консоли
«… is not a function», и узнать об этом неоткуда: обход экранов такую кнопку не
нажимает, а разметка выглядит целой.

Самый дорогой способ получить это разом — добавить файл экрана в `screens/` и
забыть тег `<script src>` в index.html: мёртвыми становятся ВСЕ кнопки этого
экрана сразу. Поэтому связность файлов проверяется здесь же.

ПРО ИЗМЕРИТЕЛЬ. Имя после точки — это метод, а не функция приложения:
`d.replace(…)`, `event.stopPropagation()`, `document.getElementById(…)`. Первая
редакция меры их не отличала и выдала одиннадцать «ненайденных функций», из
которых настоящей не было ни одной. Поэтому вызовом считается имя, перед
которым нет точки, и отдельный пробник следит, чтобы метод не вернулся в
находки.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, screen_files

# Имена — только ASCII: в Python `\w` в строковом шаблоне ловит и
# кириллицу, и пробник с русским именем «нашёл функцию».
_CALL = re.compile(r"(?<![A-Za-z0-9_$.])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_ATTR = re.compile(
    r'on(?:click|change|input|submit|keyup|keydown|focus|blur)="([^"]*)"')

#: Встроенное в язык и в браузер — определять его приложению не нужно.
_BUILTIN = {"if", "for", "while", "switch", "catch", "return", "typeof", "new",
            "function", "String", "Number", "Boolean", "Array", "Object",
            "parseInt", "parseFloat", "alert", "confirm", "prompt",
            "setTimeout", "clearTimeout", "encodeURIComponent",
            "decodeURIComponent", "JSON", "Math", "Date", "Promise", "RegExp",
            "isNaN"}


def _sources() -> str:
    return miniapp_html() + "\n" + "\n".join(
        p.read_text(encoding="utf-8") for p in screen_files())


def _defined(src: str) -> set[str]:
    out = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", src))
    out |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[\(A-Za-z_$]", src))
    out |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", src))
    return out


def _handlers(html: str) -> dict[str, int]:
    calls: dict[str, int] = {}
    for m in _ATTR.finditer(html):
        for name in _CALL.findall(m.group(1)):
            if name in _BUILTIN:
                continue
            calls[name] = calls.get(name, 0) + 1
    return calls


def test_the_detector_sees_the_markup_at_all():
    """«Мёртвых кнопок нет» на трёх обработчиках не значит ничего."""
    calls = _handlers(miniapp_html())
    assert sum(calls.values()) >= 1000 and len(calls) >= 500, (
        f"разбор нашёл всего {sum(calls.values())} вызовов в "
        f"{len(calls)} именах — сломан разбор, а не разметка")


def test_every_markup_handler_resolves():
    defined = _defined(_sources())
    calls = _handlers(miniapp_html())
    dead = sorted((n, c) for n, c in calls.items() if n not in defined)
    assert not dead, (
        f"мёртвые кнопки, обработчика нет нигде (проверено {len(calls)} имён): "
        f"{dead[:8]}")


def test_every_screen_file_is_linked_from_the_page():
    """Файл без тега делает мёртвыми ВСЕ кнопки своего экрана разом."""
    html = miniapp_html()
    linked = set(re.findall(r'<script src="screens/([\w.-]+)"', html))
    files = {p.name for p in screen_files()}
    assert files, "файлов экранов не найдено — сломан разбор"
    missing = sorted(files - linked)
    assert not missing, (
        f"файлы экранов не подключены к странице: {missing} — все кнопки этих "
        "экранов мертвы")


def test_a_method_call_is_not_mistaken_for_a_function():
    """`d.replace(…)` и `event.stopPropagation()` — не функции приложения.

    Первая редакция меры их не отличала и выдала одиннадцать находок, среди
    которых настоящей не было ни одной.
    """
    calls = _handlers('<div onclick="event.stopPropagation(); d.replace(1)"></div>')
    assert calls == {}, f"метод принят за функцию приложения: {calls}"


def test_the_detector_still_fails_on_a_missing_function():
    """Мера, которая не может найти, ничего не доказывает своим нулём."""
    calls = _handlers('<button onclick="openNowhereAtAll(1)">x</button>')
    assert calls == {"openNowhereAtAll": 1}
    assert "openNowhereAtAll" not in _defined(_sources()), (
        "выдуманное имя вдруг нашлось в исходнике — пробник бесполезен")
