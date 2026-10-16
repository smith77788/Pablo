"""Исходник мини-аппа целиком: index.html плюс вынесенные экраны.

Мини-апп перестал быть одним файлом. `index.html` пилится на
`mini_app/screens/*.js` — монолит был 1.4 МБ, и каждая правка в нём рискованнее
правки в модуле. Тест, который читает ТОЛЬКО index.html, после каждого выноса
начинает падать не потому, что функциональность пропала, а потому, что она
переехала в соседний файл.

Поэтому проверки «есть ли в мини-аппе такой контрол / такая функция» должны
брать источник отсюда:

    from tests.miniapp_source import miniapp_source
    UI = miniapp_source()

Если проверка про РАЗМЕТКУ (id, onclick, структура экрана) — берите
`miniapp_html()`: разметка живёт только в index.html, и подмешивать к ней JS
незачем.
"""
from __future__ import annotations

import functools
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MINI_APP = ROOT / "mini_app"


@functools.lru_cache(maxsize=1)
def miniapp_html() -> str:
    """Только разметка — index.html как есть."""
    return (MINI_APP / "index.html").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def screen_files() -> tuple[pathlib.Path, ...]:
    """Вынесенные экраны, в порядке подключения в index.html.

    Порядок важен: файлы — обычные классические скрипты, они делят одну
    глобальную область и грузятся ПОСЛЕ главного блока.
    """
    return tuple(sorted((MINI_APP / "screens").glob("*.js")))


@functools.lru_cache(maxsize=1)
def miniapp_source() -> str:
    """Разметка и весь JS мини-аппа одной строкой."""
    parts = [miniapp_html()]
    parts += [p.read_text(encoding="utf-8") for p in screen_files()]
    return "\n".join(parts)


def source_of(function_name: str) -> str:
    """Файл, в котором объявлена функция, — целиком.

    Нужно там, где тест разбирает тело функции: склеенный источник для этого не
    годится, границы файлов в нём условны.
    """
    import re

    pat = re.compile(r"^(?:async )?function " + re.escape(function_name) + r"\s*\(", re.M)
    for text in (miniapp_html(), *(p.read_text(encoding="utf-8") for p in screen_files())):
        if pat.search(text):
            return text
    raise AssertionError(f"функция {function_name} не найдена ни в одном файле мини-аппа")


# ── Тело ОДНОЙ функции фронта по точным границам ─────────────────────────────
#
# Десятки тестов вычленяли кусок фронта «от маркера до маркера»:
#
#     seg = html[html.index("function accReasonHuman("):]
#     seg = seg[:seg.index("function buildAccDetail")]
#
# Пока фронт был одним файлом, это работало случайно: функции шли подряд.
# После выноса экранов второй маркер уезжает в соседний файл, и срез либо
# падает (`substring not found`), либо — что хуже — становится ПУСТЫМ или,
# наоборот, захватывает десять тысяч строк и проверка делается вакуумной:
# ровно так шесть полей аналитики «проверялись» против 10551 строки.
#
# Поэтому границы берём не по соседям, а по скобкам: сканер пропускает
# комментарии, кавычки и шаблонные строки, поэтому `'{'` в тексте и `//` внутри
# `https://` его не сбивают. Самопроверка сканера — в
# tests/test_js_function_door.py: тела всех функций фронта не перекрываются и ни
# одно не перехлёстывает в следующее объявление.


def _skip_quoted(src: str, i: int) -> int:
    """За закрывающую кавычку. Незакрытая кавычка — не строка, а апостроф."""
    quote = src[i]
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        if c == "\n":
            return i + 1
        i += 1
    return i


def _skip_template(src: str, i: int) -> int:
    """За закрывающий обратный апостроф, вместе со вставками ${...}."""
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and src[i + 1:i + 2] == "{":
            i = _skip_braces(src, i + 1)
            continue
        i += 1
    return i


_EXPR_WORDS = frozenset((
    "return", "typeof", "case", "in", "of", "new", "delete", "void", "do",
    "else", "instanceof", "yield", "await",
))


def _regex_position(src: str, i: int) -> bool:
    """Косая черта на месте, где ждут выражение, открывает регексп, не деление."""
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if src[j] in "(,=:[!&|?{};+-*%~^<>":
        return True
    k = j
    while k >= 0 and (src[k].isalnum() or src[k] in "_$"):
        k -= 1
    return src[k + 1:j + 1] in _EXPR_WORDS


def _skip_regex(src: str, i: int) -> int:
    """За закрывающую косую черту регекспа. Внутри [...] косая — обычный символ."""
    i += 1
    in_class = False
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            return i
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            return i + 1
        i += 1
    return i


def _skip_braces(src: str, i: int) -> int:
    """За парную закрывающую скобку к src[i] == "{"."""
    assert src[i] == "{", src[i:i + 40]
    depth = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n:
            nxt = src[i + 1]
            if nxt == "/":
                j = src.find("\n", i)
                i = n if j < 0 else j + 1
                continue
            if nxt == "*":
                j = src.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            # `/^https?:\\/\\//` — экранированные косые внутри регекспа выглядят
            # как начало комментария; без этой ветки сканер съедал всё до
            # конца строки и терял закрывающую скобку функции.
            if _regex_position(src, i):
                i = _skip_regex(src, i)
                continue
        if c in "\"'":
            i = _skip_quoted(src, i)
            continue
        if c == "`":
            i = _skip_template(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("не нашли парную скобку")


def js_function(name: str) -> str:
    """Функция фронта от слова `function` до её закрывающей скобки.

    Зеркало `api_function` для JS: тест, которому нужно тело, получает ровно
    тело — ни соседей, ни пустоты.
    """
    import re

    pat = re.compile(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(")
    for text in (miniapp_html(), *(p.read_text(encoding="utf-8") for p in screen_files())):
        m = pat.search(text)
        if not m:
            continue
        brace = text.index("{", m.end() - 1)
        return text[m.start():_skip_braces(text, brace)]
    raise AssertionError(f"функция {name} не найдена ни в одном файле мини-аппа")


# ── Тело функции в ЛЮБОМ файле репозитория ──────────────────────────────────
#
# api_function знает только слой мини-аппа, а срезы «от маркера до маркера»
# есть и по services/account_manager.py, services/op_worker.py и прочим. Та же
# ловушка: в test_channel_resolve_invalid_object окно было задано длиной
# (`src[i:i + 4000]`) при теле post_to_channel в 146 строк, и проверка
# «резолвим через PeerChannel» проходила на выпотрошенной функции — её
# выполняли соседи, попавшие в окно.


def py_function(rel_path: str, name: str) -> str:
    """Тело функции по ТОЧНЫМ границам (ast) в указанном файле репозитория."""
    import ast

    path = ROOT / rel_path
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"функция {name} не найдена в {rel_path}")


# ── Серверная сторона: API-слой тоже перестал быть одним файлом ──────────────
#
# Тот же урок, что и выше, но про сервер. `services/mini_app_api.py` разросся до
# 25 тысяч строк и пилится на `services/mini_app_*.py`. Ратчет, который читает
# ТОЛЬКО mini_app_api.py, после каждого выноса краснеет не потому, что ручка
# пропала, а потому, что она переехала — так за один вынос прокси упали три
# проверки, ни одна из которых не про прокси. Реестр маршрутов и «есть ли в API
# такая ручка» берите отсюда.

SERVICES = ROOT / "services"


@functools.lru_cache(maxsize=1)
def api_files() -> tuple[pathlib.Path, ...]:
    """Все файлы API-слоя мини-аппа: главный плюс вынесенные модули."""
    return tuple(sorted(SERVICES.glob("mini_app_*.py")))


@functools.lru_cache(maxsize=1)
def api_source() -> str:
    """Весь API-слой одной строкой."""
    return "\n".join(p.read_text(encoding="utf-8") for p in api_files())


@functools.lru_cache(maxsize=1)
def api_routes() -> frozenset[str]:
    """Пути, зарегистрированные где-либо в API-слое мини-аппа."""
    import re

    return frozenset(re.findall(
        r'app\.router\.add_(?:get|post|put|delete|patch)\("([^"]+)"', api_source()))


def api_function(name: str) -> str:
    """Тело функции API-слоя по ТОЧНЫМ границам (ast), в любом его модуле.

    Срез фиксированной длины и «до следующего `async def`» промахиваются на
    каждой правке выше, а привязка к файлу — на каждом выносе ручки в
    отдельный модуль. Границы знает только парсер, файл ищем сами.
    """
    import ast

    for path in api_files():
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name):
                return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"функция {name} не найдена ни в одном модуле API-слоя")
