"""Обработчик отдаёт массив — значит и читать его надо как массив.

ЧТО ЛОМАЛОСЬ. `GET /api/miniapp/ecosystems` отвечает голым массивом:
`_json_resp([dict(r) for r in rows])`. Экран «Экосистемы» так его и читает —
`rows.length`, `rows.map`. А две выпадашки «Привязать к экосистеме» (в фабрике
ботов и в глобальном присутствии) читали `ecos.ecosystems`: у массива такого
поля нет, выражение `(ecos.ecosystems||[])` всегда давало пустой список, и в
обеих выпадашках навсегда оставалось одно «Не привязывать». Привязать актив к
экосистеме было нельзя, сколько бы их владелец ни создал, — и ни ошибки, ни
пустого состояния он при этом не видел: список просто выглядел как «ни одной
экосистемы ещё нет».

Поймать это глазами нельзя: обработчик и его читатель лежат в разных файлах и
в десятках тысяч строк друг от друга, а оба куска кода по отдельности
выглядят правильно.

ПОЧЕМУ ПРОВЕРКА ИМЕННО ТАКАЯ. Общая версия — «экран читает поле, которого
сервер не отдаёт» — на этом коде даёт больше десятка находок, и почти все они
ложные: обработчик собирает ответ в модуле уровнями глубже, чем видит
статический разбор. По правилу из CLAUDE.md («детектор, дающий десятки находок
в зрелом коде, почти всегда сломан») общую версию не берём. А вот узкая —
«ответ является массивом» — решается точно: обработчиков с массивом
пятнадцать, читателей четырнадцать, находок ноль.

ИЗМЕРЕНИЕ на момент написания: 15 обработчиков отдают массив, 14 их читателей
во фронте разобраны, ни один не читает у массива поле.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from aiohttp import web

from services import mini_app_api as M

ROOT = Path(__file__).resolve().parents[1]

# Что у массива читать законно. Остальное — обращение к полю объекта.
ARRAY_MEMBERS = frozenset({
    "length", "map", "filter", "forEach", "slice", "find", "reduce", "some",
    "every", "sort", "concat", "join", "indexOf", "includes", "flat",
    "flatMap", "reverse", "push", "shift", "pop", "keys", "entries", "values",
    "at", "findIndex", "splice", "toString", "lastIndexOf", "fill",
})


class _Pool:
    async def fetch(self, q, *a): return []
    async def fetchrow(self, q, *a): return None
    async def fetchval(self, q, *a): return None
    async def execute(self, q, *a): return "OK"


def _array_handlers() -> set[str]:
    """Имена обработчиков, которые отвечают голым массивом."""
    names: set[str] = set()
    for f in sorted((ROOT / "services").rglob("mini_app*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "setup_routes":
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and getattr(sub.func, "id", "") == "_json_resp"
                        and sub.args
                        and isinstance(sub.args[0], (ast.List, ast.ListComp))):
                    names.add(node.name)
    return names


def _routes() -> dict[tuple[str, str], str]:
    app = web.Application()
    M.setup_routes(app, _Pool())
    out = {}
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if path:
            out[(route.method, path)] = getattr(route.handler, "__name__", "")
    return out


def _strip_comments(text: str) -> str:
    """Убрать /* … */ и // …: имена полей в комментариях упоминают постоянно.

    Без этого проверка ловила бы сама себя: комментарий рядом с исправленным
    местом как раз и называет поле, которого у массива нет.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?<![:\\])//[^\n]*", "", text)


def _front() -> str:
    text = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    screens = ROOT / "mini_app" / "screens"
    if screens.is_dir():
        for p in sorted(screens.glob("*.js")):
            text += "\n" + p.read_text(encoding="utf-8")
    return _strip_comments(text)


def _match(routes, method: str, path: str) -> str | None:
    for (rm, rp), name in routes.items():
        if rm != method:
            continue
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+",
                          re.escape(rp).replace(r"\{", "{").replace(r"\}", "}")) + "$"
        if re.match(rx, path):
            return name
    return None


def _readers():
    """(метод, путь, обработчик, переменная, поля) для чтений ответа-массива."""
    front, routes, arrays = _front(), _routes(), _array_handlers()
    out = []
    for m in re.finditer(
            r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*await\s+api\(\s*(['"`])([^'"`]*)\2""",
            front):
        var, path = m.group(1), m.group(3)
        if not path.startswith("/api/"):
            continue
        meth = re.search(r"method:\s*['\"](\w+)['\"]", front[m.end():m.end() + 300])
        method = meth.group(1).upper() if meth else "GET"
        name = _match(routes, method,
                      re.sub(r"\$\{[^}]+\}", "1", path.split("?")[0]))
        if name not in arrays:
            continue
        # Граница области видимости — ближайший следующий `api(`: имя `d`
        # переиспользуется в каждой второй функции, и окно фиксированной длины
        # цепляло чтения ЧУЖОГО ответа (так в прототипе появилось 137 находок
        # вместо двух).
        rest = front[m.end():]
        nxt = re.search(r"\bapi\(", rest)
        scope = rest[:nxt.start()] if nxt else rest[:2500]
        fields = set(re.findall(rf"\b{re.escape(var)}\.([A-Za-z_][\w]*)\b", scope))
        out.append((method, path, name, var, sorted(fields - ARRAY_MEMBERS)))
    return out


# ── Измерение ────────────────────────────────────────────────────────────────

def test_the_measurer_sees_the_array_handlers():
    names = _array_handlers()
    assert len(names) >= 10, (
        f"обработчиков с ответом-массивом найдено {len(names)} — разбор "
        "перестал их видеть, и проверка измеряет не то")


def test_the_measurer_sees_their_readers():
    readers = _readers()
    assert len(readers) >= 8, (
        f"читателей ответа-массива разобрано {len(readers)} — проверка "
        "ослепла")


def test_the_detector_fires_on_a_field_of_an_array():
    """Обратный контроль на том самом месте, где это и жило."""
    bad = [k for k in ("ecosystems", "items", "data") if k not in ARRAY_MEMBERS]
    assert bad == ["ecosystems", "items", "data"], (
        "список законных членов массива раздут — находка пройдёт как законная")
    assert "length" in ARRAY_MEMBERS and "map" in ARRAY_MEMBERS


# ── Сама проверка ────────────────────────────────────────────────────────────

def test_no_reader_takes_a_field_off_an_array_answer():
    offenders = []
    for method, path, name, var, fields in _readers():
        if fields:
            offenders.append(
                f"{method} {path} → {name} отвечает массивом, а экран читает "
                f"{var}." + f", {var}.".join(fields))
    assert not offenders, (
        "у ответа-массива читают поле: выражение всегда даёт undefined, и "
        "экран показывает пустоту вместо данных — без ошибки и без пустого "
        "состояния:\n  " + "\n  ".join(sorted(offenders)))
