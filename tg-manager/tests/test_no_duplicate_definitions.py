"""Страховка от коллизий параллельных агентов: дубли определений тихо затеняют
друг друга (в Python/JS побеждает последнее), ломая функциональность без ошибки
компиляции.

Реальный случай (2026-07-16): два агента независимо определили async def
topology_links() в mini_app_api.py — второй затенил первый, роут забиндился на
неверную версию ({links} вместо {accounts,bots}), и «Карта связей» всегда
показывала «Связей пока нет». Синтаксис валиден, тесты по отдельным модулям
зелёные — поймать можно только проверкой на дубли имён.

Второй реальный случай (эта сессия): services/ecosystem_brain.py — ДВЕ
реализации auto_discover_members на уровне модуля, с НЕСОВМЕСТИМЫМ форматом
ответа ({object_type: count} против {added, skipped, total}). Все реальные
вызывающие (mini_app_api.py, bot/handlers/ecosystems.py, 4 места) рассчитаны
на первую версию, а из-за тени получали вторую — бот всегда писал «объекты
добавлены вручную» вместо честного списка, даже когда объекты реально
добавились. Прежняя проверка (test_no_duplicate_api_handlers) видела только
mini_app_api.py — этот класс дублей в остальных services/*.py, bot/**/*.py,
database/*.py проходил незамеченным. Отсюда — ratchet ниже.

Эти тесты ловят тот же класс на будущее.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_duplicate_api_handlers():
    """В create_app() все хендлеры — вложенные функции одной области видимости;
    два `async def name(request...)` с одним именем = затенение (мёртвый код +
    молчаливая смена поведения роута)."""
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    names = re.findall(r"^\s{4}async def ([a-z_][\w]*)\(request", src, re.MULTILINE)
    dups = {n: c for n, c in Counter(names).items() if c > 1}
    assert not dups, (
        "Дубли хендлеров в mini_app_api.py (затеняют друг друга, роут биндится на "
        f"последний): {dups}. Оставьте одну версию."
    )


def test_no_duplicate_registered_routes():
    """Один и тот же путь+метод зарегистрирован дважды — второй add_* побеждает,
    первый хендлер недостижим."""
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    routes = re.findall(r'router\.add_(get|post|put|delete)\("([^"]+)"', src)
    key = [f"{m.upper()} {p}" for m, p in routes]
    dups = {k: c for k, c in Counter(key).items() if c > 1}
    assert not dups, f"Дубли роутов (последний побеждает): {dups}"


def test_no_duplicate_js_functions():
    """Топ-уровневые function-объявления в index.html + screens/*.js не должны
    повторяться (в одной глобальной области последнее затеняет прежние)."""
    files = [ROOT / "mini_app" / "index.html"] + sorted(
        (ROOT / "mini_app" / "screens").glob("*.js"))
    names: list[str] = []
    for f in files:
        txt = f.read_text(encoding="utf-8")
        if f.suffix == ".html":
            txt = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", txt, re.S))
        # только топ-уровневые (в начале строки) объявления — вложенные хелперы
        # с общими именами (push/render) внутри своих функций законны
        names += re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", txt,
                            re.MULTILINE)
    dups = {n: c for n, c in Counter(names).items() if c > 1}
    assert not dups, (
        f"Дубли JS-функций (index.html+screens, затеняют друг друга): {dups}. "
        "Оставьте одну версию или переименуйте."
    )


# Файлы, которые уже (законно) держат несколько объявлений с одним и тем же
# именем в РАЗНЫХ областях видимости (например, одноимённые методы в разных
# классах, или @overload) — AST-проверка ниже нарочно строже regex-версии
# выше (видит КАЖДУЮ область: модуль, класс, функцию), поэтому такие случаи
# приходится явно перечислять, а не тащить их в общий список молча.
_MODULE_DUP_ALLOWLIST: dict[str, set[str]] = {}

_WATCHED_PY_DIRS = ("services", "bot", "database")


def _iter_python_files():
    for sub in _WATCHED_PY_DIRS:
        d = ROOT / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


def test_no_duplicate_module_level_functions_anywhere_in_backend():
    """Дубль ИМЕНИ функции на уровне МОДУЛЯ (не только в mini_app_api.py) —
    второе объявление молча тенит первое: тот же класс бага, что и дубль-
    хендлеры выше, но по всему бэкенду (services/bot/database), а не только
    в одном файле. Живой случай — см. докстринг файла (ecosystem_brain.py).
    """
    offenders: dict[str, list] = {}
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        names: dict[str, list[int]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.setdefault(node.name, []).append(node.lineno)
        rel = str(path.relative_to(ROOT))
        allowed = _MODULE_DUP_ALLOWLIST.get(rel, set())
        for name, lines in names.items():
            if len(lines) > 1 and name not in allowed:
                offenders[f"{rel}:{name}"] = lines
    assert not offenders, (
        "Дубли имени функции на уровне модуля (второе объявление молча "
        f"тенит первое — Python не ошибается, просто теряет код): {offenders}. "
        "Оставьте одну версию (обычно вызывающие код рассчитаны только на "
        "одну — сверьте реальных вызывающих, прежде чем выбирать какую)."
    )
