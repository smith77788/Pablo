"""Параметр, который экран посылает, сервер обязан читать.

Экран «📇 Контакты» умел фильтр «контакты этого аккаунта»: отправлял
`account_id`, репозиторий его понимал, а обработчик списка просто не читал
запрос — и экран, обещая контакты одного аккаунта, отдавал весь справочник.
Хуже молчаливого потолка: там цифра лжёт, здесь лжёт сам срез, и по нему
принимают решения («у этого аккаунта 800 контактов» — а это все контакты).

Ту находку нашли руками и для одного экрана. Правило ниже считает тот же класс
по ВСЕМУ мини-аппу: берём каждый вызов `api()` с литеральным путём, собираем
параметры запроса, которые экран реально умеет отправить (и в строке, и через
`URLSearchParams`, в том числе собранные помощником вроде
`contactFilterParams()`), находим обработчик ЭТОГО маршрута в живом роутере и
требуем, чтобы каждый параметр в нём читался.

Маршрут ищется в поднятом `web.Application`, а не поиском по тексту: путь с
подстановкой (`/account/{acc_id}/channels`) текстом не сопоставить, а роутер
знает точно, какая функция его обслуживает.

Чего правило НЕ делает: не судит о путях, которые собраны так, что литерала не
остаётся (`'…/hits' + (qs ? '?' + qs : '')`), и не обвиняет обработчик, который
разбирает запрос целиком (`rel_url.query` без имён). Лучше не сравнить, чем
обвинить фронт — прошлые детекторы ровно на этом и ломались.
"""
from __future__ import annotations

import functools
import inspect
import re

import pytest

_QUERY_READ = re.compile(r"""query\.get\(\s*['"](\w+)['"]|query\[\s*['"](\w+)['"]""")
# Запрос, положенный в переменную: `qs = request.rel_url.query` и дальше
# `qs.get("bot_id")`. Без этого правило ослепло бы на десяти ручках — а они
# параметры читают, просто через своё имя.
_QUERY_BOUND = re.compile(r"""(\w+)\s*=\s*request\.(?:rel_url\.)?query\s*$""", re.M)
# Разбор запроса целиком: имён не видно, судить не о чем.
_OPAQUE = ("dict(request.query", "dict(request.rel_url.query")


def _read_names(src: str) -> set[str]:
    """Имена параметров, которые текст читает из запроса."""
    read = {a or b for a, b in _QUERY_READ.findall(src)}
    for var in set(_QUERY_BOUND.findall(src)):
        read |= set(re.findall(
            re.escape(var) + r"""(?:\.get\(|\[)\s*['"](\w+)['"]""", src))
    return read


@functools.lru_cache(maxsize=1)
def _api_helpers() -> dict[str, str]:
    """{имя: исходник} по всем функциям API-слоя, вложенные включая."""
    import ast

    from tests.miniapp_source import api_files

    out: dict[str, str] = {}
    for path in api_files():
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines(keepends=True)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, "".join(
                    lines[node.lineno - 1:node.end_lineno]))
    return out


def _expand(src: str, depth: int = 2) -> str:
    """Исходник обработчика вместе с помощниками слоя, которые он зовёт.

    Фильтры контактов читает не обработчик, а `_contact_filters(request)` —
    одно место для списка, выгрузки и резолвера сегмента. Правило, которое
    смотрит только на тело обработчика, объявило бы ВСЕ одиннадцать фильтров
    потерянными: детектор, красный на здоровом коде, сломан сам.
    """
    helpers = _api_helpers()
    seen: set[str] = set()
    out = [src]
    for _ in range(depth):
        for name in set(re.findall(r"\b(_\w+)\s*\(", "".join(out))):
            if name in seen or name not in helpers:
                continue
            seen.add(name)
            out.append(helpers[name])
    return "".join(out)


def _unread(params: set[str], src: str) -> set[str]:
    """Параметры, которых в исходнике обработчика не видно.

    «Разбирает запрос целиком» смотрим ТОЛЬКО в самом обработчике: общий
    помощник слоя (`_cached_user` кладёт строку запроса в ключ кэша) даёт
    `rel_url.query` половине ручек, и ослепление по развёрнутому тексту
    выключило бы правило на 31 маршруте из 59, оставив зелёный ни на чём.
    Развёртывание помощников только ДОБАВЛЯЕТ прочитанные имена.
    """
    if any(tok in src for tok in _OPAQUE):
        return set()
    read = _read_names(_expand(src))
    return {p for p in params if p not in read}


def _norm(path: str) -> str:
    """Путь без имён подстановок: /account/{acc_id}/bots -> /account/{}/bots."""
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


@functools.lru_cache(maxsize=1)
def _handlers() -> dict[str, tuple[str, str]]:
    """{нормализованный путь: (имена обработчиков, их исходники)}."""
    from aiohttp import web

    from tests.miniapp_routes import StubPool
    from services import mini_app_api as M

    app = web.Application()
    M.setup_routes(app, StubPool())
    out: dict[str, list[tuple[str, str]]] = {}
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if not path.startswith("/api/miniapp"):
            continue
        try:
            src = inspect.getsource(route.handler)
        except (OSError, TypeError):
            src = ""
        out.setdefault(_norm(path), []).append((route.handler.__name__, src))
    return {k: (", ".join(sorted({n for n, _ in v})), "\n".join(s for _, s in v))
            for k, v in out.items()}


def _usp_names(body: str) -> set[str]:
    """Имена параметров, собранных через URLSearchParams в этом тексте."""
    names: set[str] = set()
    for m in re.finditer(r"URLSearchParams\(\s*\{([^}]*)\}", body):
        names |= set(re.findall(r"(\w+)\s*:", m.group(1)))
    names |= set(re.findall(r"""\.(?:set|append)\(\s*['"](\w+)['"]""", body))
    return names


def _call_chunk(js: str, start: int, quote: str) -> str:
    """Текст вызова api(...) от его начала до закрывающей скобки или конца строки."""
    chunk = js[start:start + 400]
    depth = 0
    for i, ch in enumerate(chunk):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return chunk[:i]
        elif ch == "\n" and i:
            return chunk[:i]
    return chunk


def _path_of(chunk: str, quote: str) -> str:
    """Путь вызова: подстановки склейки и шаблона становятся {}."""
    body = chunk[chunk.index(quote) + 1:]
    body = re.sub(r"\$\{[^}]*\}", "{}", body)
    body = re.sub(quote + r"\s*\+[^+]*?\+\s*" + quote, "{}", body)
    path = body.split("?")[0]
    return re.sub(quote + r"\s*\+.*$", "{}", path).rstrip(quote).rstrip("/")


@functools.lru_cache(maxsize=1)
def _call_sites() -> tuple[dict[str, set[str]], tuple[str, ...]]:
    """({путь: отправляемые параметры}, не разобранные пути).

    Разбор идёт по ФУНКЦИИ, а не по вызову `api(...)`: самый интересный случай
    (`loadContacts`) собирает запрос в переменную и зовёт `api(url)`, то есть
    поиск по `api('/api/...` его не видит вовсе — а это ровно та функция, в
    которой и жила находка. Поэтому: если в функции ровно ОДИН разбираемый путь
    мини-аппа, все параметры функции относим к нему; если путей несколько —
    только те, что стоят в самой строке пути.
    """
    from tests.miniapp_source import js_functions

    fns = js_functions()
    known = _handlers()
    sent: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    for body in fns.values():
        lits = list(re.finditer(r"""(['"`])(/api/miniapp/)""", body))
        if not lits:
            continue
        per_lit: list[tuple[str, set[str]]] = []
        for m in lits:
            quote = m.group(1)
            chunk = _call_chunk(body, m.start(), quote)
            per_lit.append((_path_of(chunk, quote),
                            set(re.findall(r"[?&]([a-zA-Z_]\w*)=", chunk))))
        # Параметры, собранные в этой же функции или в помощнике, который она зовёт.
        built: set[str] = _usp_names(body)
        for helper in set(re.findall(r"\b(\w*[Pp]arams)\(\)", body)):
            built |= _usp_names(fns.get(helper, ""))
        paths = {p for p, _ in per_lit}
        single = len(paths) == 1 and built and "?" in body
        for path, params in per_lit:
            if single:
                params = params | built
            if not params:
                continue
            if path not in known:
                unresolved.add(path)
                continue
            sent.setdefault(path, set()).update(params)
    return sent, tuple(sorted(unresolved))


def _findings() -> list[str]:
    sent, _ = _call_sites()
    handlers = _handlers()
    out = []
    for path, params in sorted(sent.items()):
        names, src = handlers[path]
        miss = sorted(_unread(params, src))
        if miss:
            out.append(f"{path} ({names}): экран посылает {miss}, обработчик не читает")
    return out


@pytest.mark.parametrize("src,expected", [
    # читается только потолок — срез по аккаунту потерян
    ("async def h(r):\n    lim = r.query.get('limit')\n", {"account_id"}),
    # читается и срез, и потолок — обвинять не за что
    ("async def h(r):\n    acc = r.query.get('account_id')\n"
     "    lim = r.query['limit']\n", set()),
    # запрос разбирают целиком: имён не видно, судить не о чем
    ("async def h(r):\n    q = dict(request.query)\n", set()),
    # запрос положен в переменную — читается через своё имя
    ("async def h(r):\n    qs = request.rel_url.query\n"
     "    acc = qs.get('account_id')\n    lim = qs['limit']\n", set()),
    # помощник слоя читает запрос за обработчика: правило идёт в него
    ("async def h(r):\n    lim, off = _list_limit(r, 50, 200)\n"
     "    acc = r.query.get('account_id')\n", set()),
])
def test_the_rule_is_red_only_on_a_parameter_nobody_reads(src, expected):
    assert _unread({"account_id", "limit"}, src) == expected


def test_the_rule_resolves_the_calls_it_judges():
    """Пустая выдача разбора сделала бы правило зелёным ни на чём."""
    sent, unresolved = _call_sites()
    assert len(sent) >= 50, f"разобрано лишь {len(sent)} маршрутов с параметрами"
    assert len(_handlers()) >= 400, f"роутер отдал лишь {len(_handlers())} путей"
    assert len(unresolved) <= 4, f"не разобрано слишком много путей: {unresolved}"


def test_the_rule_sees_a_parameter_nobody_reads_on_every_route_it_judges():
    """Пробник: выдуманный параметр обязан стать находкой на КАЖДОМ маршруте.

    Иначе зелёный ничего не значит: ослепшая ручка молчит так же, как здоровая.
    Правило один раз уже ослепло — общий помощник слоя (`_cached_user`) дал
    `rel_url.query` половине обработчиков, и «разбирает запрос целиком»
    выключило проверку на 31 маршруте из 59.
    """
    sent, _ = _call_sites()
    handlers = _handlers()
    deaf = [p for p in sent if not _unread({"zzz_never_sent"}, handlers[p][1])]
    assert not deaf, ("правило не видит заведомо непрочитанного параметра на "
                      f"маршрутах: {deaf}")


def test_every_sent_parameter_is_read_by_its_handler():
    bad = _findings()
    assert not bad, (
        "экран отправляет параметр, которого обработчик не читает: срез молча "
        "превращается в полный список, и по нему принимают решения.\n  "
        + "\n  ".join(bad))
