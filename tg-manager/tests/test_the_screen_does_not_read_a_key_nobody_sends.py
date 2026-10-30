"""Экран не вправе читать поле, которого в ответе нет.

Близнец правила «отправленный фильтр обязан читаться», но в другую сторону. Там
проверялось, что сервер читает параметр, который ему прислали. Здесь — что
экран читает поле, которое сервер действительно отдаёт. Цена ошибки другая и
хуже: отсутствующее поле в JS не падает, а молча становится `undefined`, и дальше
`|| 0` превращает его в ноль, а `(x||'—')` — в прочерк. То есть незнание
показывается как проверенный факт.

Так и было в «удалить все невоскрешаемые аккаунты»: запасной путь читал
`st.filtered_total`, а сервер кладёт итог под фильтром в `st.page`. Запасной путь
нужен — `stats` приходит пустым словарём, когда сводный запрос не сложился, — и
вместо числа он давал ноль, то есть экран говорил «✅ Мёртвых аккаунтов нет 👍»
при полном парке мёртвых. Ровно та ошибка, от которой эта же функция уже
защищалась в ветке сетевого сбоя.

ГРАНИЦА ПРАВИЛА. Судятся только те роуты, чей ответ — словарь-литерал с
литеральными ключами: для них набор полей известен точно. Ответ, собранный в
переменной, с `**` или из `dict(row)`, правило не трогает — это честная граница,
а не полнота. Сопоставление идёт по МЕТОДУ и по шаблону пути посегментно.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap

import pytest
from aiohttp import web

from services import mini_app_api as M
from tests.miniapp_source import js_functions

# Свойства промисов, массивов и строк — не поля ответа.
_NOT_FIELDS = {
    "then", "catch", "finally", "length", "map", "forEach", "filter", "slice",
    "join", "push", "toFixed", "sort", "concat", "find", "some", "every",
    "reverse", "includes", "indexOf", "lastIndexOf", "splice", "keys", "values",
    "entries", "constructor", "hasOwnProperty", "toString", "trim", "split",
    "replace", "padStart", "pop", "shift", "flat", "at",
}


class _Pool:
    async def fetch(self, *a):
        return []

    async def fetchval(self, *a):
        return 0

    async def fetchrow(self, *a):
        return None

    async def execute(self, *a):
        return "OK"


def _literal_keys(src: str):
    """Поля ответа, если ВСЕ `_json_resp` в обработчике — словари-литералы.

    Иначе None: набор полей статически не известен, и судить обработчик нельзя.
    """
    try:
        tree = ast.parse(textwrap.dedent(src))
    except (SyntaxError, IndentationError):
        return None
    keys: set[str] = set()
    found = False
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_json_resp" and n.args):
            continue
        found = True
        arg = n.args[0]
        if not isinstance(arg, ast.Dict):
            return None
        for k in arg.keys:
            if k is None or not (isinstance(k, ast.Constant)
                                 and isinstance(k.value, str)):
                return None   # `**other` или вычисляемый ключ
            keys.add(k.value)
    return keys if found else None


def _judgeable_routes() -> dict[tuple[str, str], set[str]]:
    app = web.Application()
    M.setup_routes(app, _Pool())
    out: dict[tuple[str, str], set[str]] = {}
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter")
        if not path:
            continue
        try:
            src = inspect.getsource(route.handler)
        except (OSError, TypeError):
            continue
        keys = _literal_keys(src)
        if keys:
            out[(route.method, path)] = keys
    return out


def _arg_region(body: str, open_paren: int) -> str:
    depth = 0
    for i in range(open_paren, len(body)):
        c = body[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return body[open_paren:i + 1]
    return body[open_paren:open_paren + 600]


def _first_arg(arg: str) -> str:
    depth = 0
    for i in range(1, len(arg)):
        c = arg[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return arg[1:i]
            depth -= 1
        elif c == "," and depth == 0:
            return arg[1:i]
    return arg[1:]


_HOLE = "\x00"


def _path_pattern(arg: str):
    """Шаблон пути: литеральные куски, подстановки — дырки."""
    first = _first_arg(arg)
    chunks = re.findall(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`", first)
    if not chunks:
        return None
    parts = [re.sub(r"\$\{[^}]*\}", _HOLE, a or b or c) for a, b, c in chunks]
    pat = _HOLE.join(parts) if len(parts) > 1 else parts[0]
    # Подстановка В КОНЦЕ не оставляет замыкающего литерала: `'/api/x/'+id`
    # давало шаблон на один сегмент короче, и путь карточки сопоставлялся с
    # роутом списка. Если после последнего литерала есть склейка — дописываем
    # место под сегмент.
    last = chunks[-1][0] or chunks[-1][1] or chunks[-1][2]
    tail = first.rsplit(last, 1)[-1] if last else first
    if re.search(r"\+\s*[A-Za-z_$(]", tail) and pat.endswith("/"):
        pat += _HOLE
    pat = pat.split("?")[0].split("#")[0]
    return pat if pat.startswith("/api/") else None


def _method(arg: str) -> str:
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        if re.search(r"method:\s*['\"]" + m, arg):
            return m
    return "GET"


def _matches(pattern: str, route: str) -> bool:
    a = pattern.strip("/").split("/")
    b = route.strip("/").split("/")
    if len(a) != len(b):
        return False
    for sa, sb in zip(a, b):
        if sb.startswith("{") and sb.endswith("}"):
            continue
        if _HOLE in sa:
            if not re.fullmatch(re.escape(sa).replace("\\" + _HOLE, ".*"), sb):
                return False
        elif sa != sb:
            return False
    return True


def _strip_comments(src: str) -> str:
    """Комментарий — не код.

    Правило читало имена полей из пояснения к правке: строка «запасной путь
    читал st.filtered_total» делала находку из объяснения этой самой находки.
    """
    return "\n".join("" if ln.lstrip().startswith("//") else ln
                     for ln in src.splitlines())


_CALL = re.compile(r"(?:(?:const|let|var)\s+(\w+)\s*=\s*)?await\s+api\(")


def _scan(functions: dict[str, str], routes: dict[tuple[str, str], set[str]]):
    """(находки, сколько вызовов осуждено, сколько неоднозначных)."""
    findings, judged, ambiguous = [], 0, 0
    for name, raw in functions.items():
        body = _strip_comments(raw)
        for m in _CALL.finditer(body):
            var = m.group(1)
            if not var:
                continue
            arg = _arg_region(body, body.index("(", m.end() - 1))
            pat = _path_pattern(arg)
            if pat is None:
                continue
            meth = _method(arg)
            hits = [p for (mm, p) in routes if mm == meth and _matches(pat, p)]
            if len(hits) > 1:
                ambiguous += 1
                continue
            if not hits:
                continue   # ответ роута не литерал — такой не судим
            judged += 1
            # Читаем до СЛЕДУЮЩЕГО присваивания той же переменной: `const d =
            # await api(...)` в одной функции встречается по нескольку раз.
            after = body[m.end():]
            nxt = re.search(r"(?:const|let|var)\s+" + re.escape(var) + r"\s*=|\b"
                            + re.escape(var) + r"\s*=\s*await\s+api\(", after)
            window = after[:nxt.start()] if nxt else after
            read = set(re.findall(r"\b" + re.escape(var) + r"\.([A-Za-z_]\w*)",
                                  window)) - _NOT_FIELDS
            missing = sorted(read - routes[(meth, hits[0])])
            if missing:
                findings.append(f"{name} → {meth} {hits[0]}: {', '.join(missing)}")
    return findings, judged, ambiguous


@pytest.fixture(scope="module")
def world():
    routes = _judgeable_routes()
    return routes, _scan(js_functions(), routes)


# ── Проба: правило видит и находку, и настоящий ответ ───────────────────────

def test_the_rule_sees_a_field_nobody_sends(world):
    """На подложном экране, читающем небывалое поле, правило обязано сработать."""
    routes, _ = world
    (meth, path), keys = next(iter(
        (k, v) for k, v in routes.items() if k[0] == "GET" and "{" not in k[1]))
    good = sorted(keys)[0]
    fake = {
        "probeOk": "const d = await api('%s');\n  render(d.%s);" % (path, good),
        "probeBad": "const d = await api('%s');\n  render(d.nebyvaloe_pole);" % path,
    }
    findings, judged, _amb = _scan(fake, routes)
    assert judged == 2, "правило не сопоставило подложные вызовы с роутом"
    assert len(findings) == 1 and "probeBad" in findings[0], findings
    assert "nebyvaloe_pole" in findings[0], findings


def test_the_rule_does_not_read_names_out_of_comments(world):
    """Имя поля из комментария — не чтение поля."""
    routes, _ = world
    (meth, path), _keys = next(iter(
        (k, v) for k, v in routes.items() if k[0] == "GET" and "{" not in k[1]))
    fake = {"probeComment":
            "const d = await api('%s');\n  // раньше читали d.nebyvaloe_pole\n" % path}
    findings, judged, _ = _scan(fake, routes)
    assert judged == 1 and findings == [], findings


def test_a_card_route_is_not_mistaken_for_its_list(world):
    """`'/api/x/'+id` — это карточка, а не список: сегмент не теряется."""
    assert _path_pattern("('/api/miniapp/notary/'+id)") == "/api/miniapp/notary/\x00"
    assert _matches("/api/miniapp/notary/\x00", "/api/miniapp/notary/{watch_id}")
    assert not _matches("/api/miniapp/notary/\x00", "/api/miniapp/notary")


def test_the_rule_judges_enough_of_the_app(world):
    """Пустой список находок при пустом охвате ничего не значит."""
    routes, (_f, judged, ambiguous) = world
    assert len(routes) >= 150, (
        f"роутов с литеральным ответом всего {len(routes)} — правило ослепло")
    assert judged >= 300, (
        f"осуждено вызовов всего {judged}: правило перестало находить экраны")
    assert ambiguous == 0, (
        f"{ambiguous} вызовов сопоставились с несколькими роутами — "
        "шаблон пути стал слишком широким, и находки станут случайными")


# ── Собственно правило ─────────────────────────────────────────────────────

def test_no_screen_reads_a_field_the_server_never_sends(world):
    _routes, (findings, _j, _a) = world
    assert not findings, (
        "экран читает поле, которого в ответе нет: в JS это не падение, а "
        "undefined, и дальше `||0` покажет ноль, а `||'—'` — прочерк, то есть "
        "незнание выдаётся за факт:\n  " + "\n  ".join(findings))


# ── Та находка, с которой правило началось ─────────────────────────────────

def test_the_dead_account_count_is_taken_from_where_it_lies():
    """Итог под фильтром лежит в `page`, и «не знаю» — не ноль."""
    from tests.miniapp_source import js_function

    js = js_function("purgeDeadAccounts")
    assert "page.filtered_total" in js, (
        "запасной путь снова читает итог из корня ответа — там его нет")
    assert "cnt == null" in js, (
        "нет ветки «числа не узнали»: ноль снова означает «мёртвых нет», "
        "хотя это «не знаю»")
    # Ветка незнания идёт РАНЬШЕ радостного «мёртвых нет». Сравнивать нужно по
    # КОДУ: в catch-блоке этой же функции та же фраза стоит в комментарии про
    # прошлую правку, и порядок по сырому тексту получался обратным.
    code = _strip_comments(js)
    assert code.index("cnt == null") < code.index("Мёртвых аккаунтов нет"), code
