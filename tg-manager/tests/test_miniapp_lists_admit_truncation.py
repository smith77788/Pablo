"""Список с потолком обязан признаваться, что он не весь.

Экран, который берёт первые 30 записей из восьмидесяти и рисует их без
единого намёка, врёт молча: владелец видит «вот всё, что есть» и действует
по неполному списку. Это «скрытая неполнота» из стандарта владельца —
список без пагинации наравне с массовой операцией без прогресса.

Правило: если маршрут мини-аппа ограничивает выдачу потолком, ответ обязан
нести настоящий итог (`total`), а экран — показывать кнопку «Показать ещё».
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "services", "mini_app_api.py")
HTML = os.path.join(ROOT, "mini_app", "index.html")

# Списки, которые владелец наполняет сам: их легко перерасти, и обрыв на
# первой странице читается как «больше ничего нет».
OWNER_BUILT_LISTS = [
    "new_users", "notary_list", "keywords", "chatlist_folders_list",
    "competitors_list", "ai_memory_list", "asset_templates_list",
    "presence_packs_list", "narrative_campaigns_list",
]

# Экранные функции, где кнопка «Показать ещё» обязана быть.
SCREENS_WITH_MORE = [
    "loadCompetitors", "loadKeywords", "openNewUsers", "openNotary",
    "openFolders", "openAiMemory", "loadAssetTpl", "openPresencePacks",
    "openNarrative",
]


@functools.lru_cache(maxsize=1)
def _api_funcs() -> dict[str, str]:
    src = open(API, encoding="utf-8").read()
    lines = src.splitlines()
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


@functools.lru_cache(maxsize=1)
def _html() -> str:
    return open(HTML, encoding="utf-8").read()


def _js_func(name: str) -> str:
    """Тело экранной функции по балансу скобок — не окном фиксированной длины."""
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def test_helpers_exist():
    """Антивакуумность: без общих помощников проверки ниже ничего не значат."""
    h = _html()
    assert "function moreBtn(" in h, "общей кнопки «Показать ещё» нет"
    assert "function listOf(" in h and "function totalOf(" in h, (
        "общего чтения ответа списка нет")
    assert "def _list_limit(" in open(API, encoding="utf-8").read(), (
        "общего разбора потолка на сервере нет")
    # Заведомо здоровый пример, который был в продукте до этой проверки.
    assert "loadContacts(true)" in h, "образцовый список контактов исчез"


def test_capped_routes_return_the_real_total():
    """Потолок без настоящего итога — это молчаливый обрыв."""
    broken = []
    for name in OWNER_BUILT_LISTS:
        body = _api_funcs().get(name)
        assert body, f"маршрут {name} исчез"
        code = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("#"))
        if "_list_limit(" not in code:
            broken.append(f"{name}: потолок не подвинуть — экран не сможет догрузить")
        elif "COUNT(*)" not in code or '"total"' not in code:
            broken.append(f"{name}: в ответе нет настоящего итога (COUNT + total)")
    assert not broken, (
        "Список обрывается молча — владелец примет страницу за весь список:\n  "
        + "\n  ".join(broken))


def test_screens_offer_to_load_the_rest():
    """У списка с потолком должна быть кнопка «Показать ещё»."""
    broken = []
    for name in SCREENS_WITH_MORE:
        body = _js_func(name)
        code = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("//"))
        if "moreBtn(" not in code:
            broken.append(f"{name}: нет кнопки «Показать ещё»")
        elif "limit=" not in code:
            broken.append(f"{name}: потолок не передаётся на сервер")
    assert not broken, (
        "Экран не даёт добраться до остального списка:\n  " + "\n  ".join(broken))


def test_tiles_count_everything_not_the_page():
    """Плитка «N кампаний» считала показанное, а не всё — это тот же обман."""
    pairs = [
        ("openNarrative", "m-narr-val"),
        ("openPresencePacks", "m-pp-val"),
        ("loadAssetTpl", "m-at-val"),
        ("openAiMemory", "m-aimem-val"),
        ("loadCompetitors", "m-comp-cnt"),
        ("openNotary", "m-notary-val"),
    ]
    broken = []
    for fn, tile in pairs:
        body = _js_func(fn)
        line = next((l for l in body.splitlines()
                     if tile in l and not l.lstrip().startswith("//")), None)
        if line is None:
            broken.append(f"{fn}: плитка {tile} больше не заполняется")
        elif re.search(r"\b(rows|items|mems|us|fs|NOTARY_WATCHES)\.length", line):
            broken.append(f"{fn}: плитка {tile} считает показанную страницу, а не всё")
    assert not broken, (
        "Плитка называет число показанных записей вместо настоящего:\n  "
        + "\n  ".join(broken))


def test_load_more_handlers_exist():
    """Кнопка зовёт функцию по имени из строки — опечатка тут молчит до клика."""
    h = _html()
    names = set(re.findall(r"moreBtn\([^)]*?['\"]([A-Za-z_$][\w$]*)\s*\(", h))
    assert len(names) >= 8, f"обработчиков «Показать ещё» найдено мало: {names}"
    missing = [n for n in sorted(names)
               if not re.search(r"(?:async\s+)?function\s+" + re.escape(n) + r"\s*\(", h)]
    assert not missing, (
        "Кнопка «Показать ещё» зовёт несуществующую функцию — нажатие ничего "
        f"не сделает: {missing}")
