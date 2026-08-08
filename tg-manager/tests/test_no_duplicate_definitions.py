"""Страховка от коллизий параллельных агентов: дубли определений тихо затеняют
друг друга (в Python/JS побеждает последнее), ломая функциональность без ошибки
компиляции.

Реальный случай (2026-07-16): два агента независимо определили async def
topology_links() в mini_app_api.py — второй затенил первый, роут забиндился на
неверную версию ({links} вместо {accounts,bots}), и «Карта связей» всегда
показывала «Связей пока нет». Синтаксис валиден, тесты по отдельным модулям
зелёные — поймать можно только проверкой на дубли имён.

Эти тесты ловят тот же класс на будущее.
"""
from __future__ import annotations

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
