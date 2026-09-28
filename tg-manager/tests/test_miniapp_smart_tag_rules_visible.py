"""Правила умных тегов видны и управляемы с экрана.

Было: экран «🏷 Умные теги» показывал только готовые метки, а кнопка
«⚡ Применить» рапортовала «проверено правил: 7». Этих семи правил владелец
не мог ни увидеть, ни выключить; своё завести было нечем, хотя сервер это
умел (`/uch/smart-tags/rules` — GET/POST/DELETE/PUT toggle, все четыре
маршрута с фронта никто не звал).

Отдельная мина: у встроенного правила id — строка «builtin_premium», а
маршрут toggle/delete делает `int(rule_id)`. Кнопки «выключить»/«удалить»
поэтому допустимы только у своих правил, иначе экран получит сырой 500.
"""
from __future__ import annotations

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
    """Тело экранной функции по балансу скобок — не окном фиксированной длины."""
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def test_screen_reads_the_rules():
    body = _js_func("openUchSmartTags")
    assert "/api/miniapp/uch/smart-tags/rules" in body, (
        "экран по-прежнему не показывает правила")
    assert "/api/miniapp/uch/smart-tags'" in body or \
           '/api/miniapp/uch/smart-tags"' in body, "пропали сами метки"


def test_rules_can_be_created_toggled_and_deleted():
    create = _js_func("stCreateRule")
    assert "method:'POST'" in create.replace(" ", "")
    assert "/api/miniapp/uch/smart-tags/rules" in create
    assert "conditions" in create, "правило без условия не имеет смысла"

    tog = _js_func("stToggleRule")
    assert "/toggle" in tog and "PUT" in tog

    dele = _js_func("stDeleteRule")
    assert "DELETE" in dele
    assert "confirmDelete" in dele, "удаление правила без подтверждения"


def test_builtin_rules_offer_no_broken_buttons():
    """У встроенного правила id не число — toggle/delete вернут 500."""
    body = _js_func("openUchSmartTags")
    i = body.find("builtin.forEach")
    assert i > 0, "встроенные правила на экране не выводятся"
    j = body.find("});", i)
    assert j > i
    block = body[i:j]
    assert "stToggleRule" not in block, "кнопка «выключить» у встроенного правила"
    assert "stDeleteRule" not in block, "кнопка «удалить» у встроенного правила"


def test_condition_is_written_in_russian():
    """Владелец не читает по-английски: поля и операторы подписаны словами."""
    h = _html()
    for table in ("ST_FIELDS", "ST_OPS"):
        m = re.search(table + r"\s*=\s*\{(.*?)\n\};", h, re.S)
        assert m, f"таблица {table} не найдена"
        vals = re.findall(r"'([^']{2,})'", m.group(1))
        human = [v for v in vals if not re.fullmatch(r"[a-z_]+", v)]
        assert human, f"{table} без человеческих подписей"
        for v in human:
            # «Username» и «Premium» — термины самого Telegram, они уже стоят
            # в остальном интерфейсе; всё прочее обязано быть по-русски.
            if v.replace("Username", "").replace("Premium", "").strip(" -(@…)") == "":
                continue
            assert re.search(r"[А-Яа-яЁё]", v), f"английская подпись в {table}: {v}"
    assert "function stCondText" in h, "условие правила нечем показать словами"


def test_apply_reports_what_actually_changed():
    """«Применено: N» раньше считало попытки. Теперь — новые метки и совпадения."""
    body = _js_func("applySmartTags")
    assert "d.matched" in body, "экран не показывает, сколько совпало"
    assert "d.applied" in body
    assert re.search(r"applied\s*=\s*await|api\(", body)
    src = open(API, encoding="utf-8").read()
    assert "uch_smart_tags_apply" in src


def test_error_has_a_way_out():
    body = _js_func("openUchSmartTags")
    assert "errHtml(e.message, 'openUchSmartTags()')" in body, (
        "ошибка на экране правил оставляет тупик")
