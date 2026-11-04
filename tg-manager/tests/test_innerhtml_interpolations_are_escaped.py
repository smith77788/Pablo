"""Храповик: новые подстановки в innerHTML без esc() не появляются.

Мини-апп рисует чужие данные (имена и био из парсинга, тексты сообщений,
имена ботов) через innerHTML, а CSP пока разрешает 'unsafe-inline'. Значит,
одна подстановка серверной строки без экранирования — это выполнение чужого
скрипта в сессии владельца, то есть его токен и его флот.

Проверка эвристическая и честно ограничена: она смотрит строки, где
присваивание innerHTML и шаблон стоят в ОДНОЙ строке, и считает подстановки
`${…}`, не обёрнутые в esc()/escBadge(). Все найденные на 2026-11-02 разобраны
вручную: это числа, id, константы интерфейса и тернарники над ними; две
серверные строки (имя бота в графике, эмодзи губернатора) обёрнуты в esc().
Многострочные шаблоны эта проверка не видит.

Если число выросло: оберните новую подстановку в esc(), а если это
действительно число или константа — поднимите BASELINE с пояснением в коммите.
"""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = 51
_SAFE = re.compile(r"(esc|escBadge|escJs)\s*\(")


def _count() -> tuple[int, list[str]]:
    files = [os.path.join(ROOT, "mini_app", "index.html")] + sorted(
        glob.glob(os.path.join(ROOT, "mini_app", "screens", "*.js")))
    n, where = 0, []
    for f in files:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh, 1):
                if "innerHTML" not in line:
                    continue
                for m in re.finditer(r"\$\{\s*([^}]*)", line):
                    if not _SAFE.match(m.group(1).strip()):
                        n += 1
                        where.append(f"{os.path.basename(f)}:{i}: ${{{m.group(1).strip()[:60]}}}")
    return n, where


def test_unescaped_innerhtml_interpolations_do_not_grow():
    n, where = _count()
    assert n <= BASELINE, (
        f"подстановок в innerHTML без esc() стало {n} (было {BASELINE}). "
        "Оберните новые в esc() или поднимите BASELINE с объяснением.\n"
        + "\n".join(where[-20:]))
