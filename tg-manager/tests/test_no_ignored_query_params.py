"""Экран послал параметр, а обработчик его не читает — тихий отказ.

Класс, который уже дважды находился в этом продукте: клиент добавляет к адресу
`?search=…` или `?limit=…`, сервер параметр не смотрит и отдаёт то же, что и
без него. Снаружи это выглядит как работающий фильтр: человек сузил список,
список изменился (потому что отрисовался заново), а показано то же самое. Или
наоборот: «показать ещё» просит больший потолок, сервер держит свой, и список
обрывается молча — тридцать из восьмидесяти выглядят как «всё, что есть».

Поймать это тестом можно, потому что обе стороны лежат в репозитории: адреса с
параметрами берутся из мини-аппа, обработчик — у РОУТЕРА собранного приложения
(не текстом по файлу: модули мини-аппа режутся, и текстовый поиск врал бы).

Сам этот тест при написании сначала «нашёл» четырнадцать пропущенных `limit` —
все ложные: потолок читается общим помощником `_list_limit(request, …)`, а не по
имени параметра. Поэтому ниже есть проверка самого измерителя: она подсовывает
обработчик, который параметр действительно не читает, и требует, чтобы он был
замечен.
"""
from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]

# Помощники, которые читают параметр вместо обработчика.
VIA_HELPER = {
    "limit": re.compile(r"_(?:list_limit|effective_limit)\("),
    "offset": re.compile(r"_(?:list_offset|paging)\("),
}


class _StubPool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return 0

    async def execute(self, q, *a):
        return "OK"


def _client_source() -> str:
    src = (ROOT / "mini_app" / "index.html").read_text("utf-8")
    screens = ROOT / "mini_app" / "screens"
    if screens.is_dir():
        src += "\n" + "\n".join(p.read_text("utf-8") for p in sorted(screens.glob("*.js")))
    return src


def _requested_params() -> dict[str, set[str]]:
    """Путь → имена параметров, которые мини-апп к нему добавляет."""
    out: dict[str, set[str]] = collections.defaultdict(set)
    for m in re.finditer(
            r"""\b(?:api|fetchT)\(\s*[`'"](/api/miniapp/[^`'"]*\?[^`'"]*)[`'"]""",
            _client_source()):
        path, _, qs = m.group(1).partition("?")
        path = re.sub(r"\$\{[^}]+\}", "1", path).rstrip("/")
        for key in re.findall(r"[?&]([a-zA-Z_][a-zA-Z0-9_]*)=", "?" + qs):
            out[path].add(key)
    return out


def _handler_names() -> list[tuple[re.Pattern, str]]:
    from services import mini_app_api as M

    app = web.Application()
    M.setup_routes(app, _StubPool())
    pairs = []
    for route in app.router.routes():
        if route.method not in ("GET", "POST"):
            continue
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if not path.startswith("/api/miniapp/"):
            continue
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+",
                          re.escape(path).replace(r"\{", "{").replace(r"\}", "}")) + "$"
        pairs.append((re.compile(rx), getattr(route.handler, "__name__", "")))
    return pairs


def _function_bodies() -> dict[str, str]:
    bodies: dict[str, str] = {}
    for p in sorted((ROOT / "services").glob("mini_app*.py")):
        src = p.read_text("utf-8")
        lines = src.split("\n")
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
                bodies.setdefault(n.name, "\n".join(lines[n.lineno - 1:n.end_lineno]))
    return bodies


def _reads(body: str, key: str) -> bool:
    if re.search(rf"""['"]{re.escape(key)}['"]""", body):
        return True
    helper = VIA_HELPER.get(key)
    return bool(helper and helper.search(body))


def test_the_probe_sees_both_sides():
    """Страховка измерителя: без адресов или без обработчиков тест беспредметен."""
    assert len(_requested_params()) > 20, "в мини-аппе не найдены адреса с параметрами"
    assert len(_handler_names()) > 500, "инвентарь маршрутов пуст"
    assert len(_function_bodies()) > 500, "тела обработчиков не разобраны"


def test_the_probe_notices_a_handler_that_ignores_the_parameter():
    """Главная опасность здесь — молчаливое «всё хорошо»."""
    assert not _reads("    rows = await pool.fetch(SQL, uid)", "search")
    assert _reads("    q = request.query.get('search')", "search")
    # Потолок, прочитанный общим помощником, пропускать нужно — на этом
    # измеритель и ошибся при первом запуске.
    assert _reads("    n = _list_limit(request, 50, 1000)", "limit")
    assert not _reads("    n = 50", "limit")


def test_every_parameter_the_screen_sends_is_read():
    handlers = _handler_names()
    bodies = _function_bodies()
    ignored = []
    for path, keys in sorted(_requested_params().items()):
        name = next((n for rx, n in handlers if rx.match(path)), None)
        if not name or name not in bodies:
            continue
        for key in sorted(keys):
            if not _reads(bodies[name], key):
                ignored.append(f"{path}?{key}= → {name}()")
    assert not ignored, (
        "экран посылает параметр, а обработчик его не читает — фильтр или "
        "потолок молча не работает:\n" + "\n".join(ignored[:20]))
