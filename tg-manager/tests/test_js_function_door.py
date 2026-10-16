"""Дверь к телу одной функции фронта: проверяем сканер, а не код через сканер.

Десятки проверок вычленяли кусок мини-аппа «от маркера до маркера»
(`html[html.index("function accReasonHuman("):html.index("function buildAccDetail")]`).
Пока фронт был одним файлом, соседство функций держалось случайно; после
выноса экранов в `mini_app/screens/*.js` второй маркер уезжает в другой файл —
и срез либо падает, либо становится вакуумным. Так шесть полей аналитики
«проверялись» против 10551 строки: проверка была зелёной всегда.

`miniapp_source.js_function` берёт границы по скобкам. Скобочный сканер — вещь,
которую легко сломать незаметно: кавычка в регекспе, `//` внутри `https://`,
`'{'` в тексте. Поэтому здесь не «работает ли дверь на одном примере», а
прогон по ВСЕМ функциям фронта с двумя инвариантами: тела не перекрываются и
ни одно не перехлёстывает в следующее объявление. Перехлёст — это ровно та
вакуумность, от которой дверь и спасает.

Обратный контроль (16.10.2026): до ветки про регекспы этот же прогон давал 15
находок в 12 функциях — `/[&<>"]/g` открывал мнимую строку, а `\\/\\//` в конце
`/^https?:\\/\\//` читался как начало комментария и сканер съедал всё до конца
строки. То есть измеритель ловит поломку сканера, а не молчит о ней.
"""
from __future__ import annotations

import re

from tests.miniapp_source import (
    _skip_braces,
    js_function,
    miniapp_html,
    screen_files,
)

_DECL = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)


def _files() -> list[tuple[str, str]]:
    out = [("index.html", miniapp_html())]
    out += [(p.name, p.read_text(encoding="utf-8")) for p in screen_files()]
    return out


def test_there_are_functions_to_measure():
    """Самопроверка: пустой список функций сделал бы весь файл зелёным зря."""
    total = sum(len(_DECL.findall(text)) for _, text in _files())
    assert total > 1000, f"функций фронта найдено {total} — измеритель сломан"
    assert len(_files()) > 5, "вынесенные экраны не читаются"


def test_every_function_body_is_brace_balanced():
    """Сканер обязан находить парную скобку у каждой функции фронта."""
    for label, text in _files():
        for m in _DECL.finditer(text):
            brace = text.index("{", m.end() - 1)
            end = _skip_braces(text, brace)   # бросит, если пары нет
            assert end > brace + 1, f"{label}: {m.group(1)} — пустое тело"


def test_no_body_spills_into_the_next_function():
    """Перехлёст — это вакуумная проверка: в срез попадает пол-приложения."""
    for label, text in _files():
        decls = [(m.start(), m.end(), m.group(1)) for m in _DECL.finditer(text)]
        for k, (start, after_name, name) in enumerate(decls):
            end = _skip_braces(text, text.index("{", after_name - 1))
            nxt = decls[k + 1][0] if k + 1 < len(decls) else len(text)
            assert end <= nxt, (
                f"{label}: тело {name} перехлестнуло в {decls[k + 1][2]}"
            )
            if k:
                prev_end = _skip_braces(
                    text, text.index("{", decls[k - 1][1] - 1))
                assert prev_end <= start, f"{label}: {name} перекрыт предыдущим"


def test_the_door_finds_a_function_in_a_split_out_screen():
    """Смысл двери: функция живёт уже не в index.html, а в screens/*.js."""
    body = js_function("renderAccounts")
    assert body.startswith("function renderAccounts("), body[:60]
    assert body.rstrip().endswith("}")
    assert "function loadMoreAccounts" not in body, "захвачена соседняя функция"


def test_the_door_refuses_a_name_it_cannot_find():
    try:
        js_function("этойФункцииНет")
    except AssertionError:
        return
    raise AssertionError("дверь молча вернула чужое тело")
