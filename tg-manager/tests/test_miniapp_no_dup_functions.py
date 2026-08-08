"""Регрессия: в mini_app (index.html + вынесенные screens/*.js) не должно
быть дублей имён функций.

Две `function X` в одном скоупе → вторая молча затеняет первую, и все вызовы
уходят в неё. Так `toggleFunnel` (обычные Воронки) была сломана — её затеняла
одноимённая функция авто-воронок (бьющая в /auto_funnel вместо /funnel).

mini_app/screens/*.js — точечно вынесенные логически обособленные экраны
(см. docs/CLAUDE.md → «Известный технический долг»). Они подключаются как
обычные classic <script src> ПОСЛЕ основного inline-скрипта index.html и
живут в ТОЙ ЖЕ глобальной области видимости (top-level `function`/`let` в
classic-скриптах — это общий глобальный scope документа, не per-file). Значит
риск того же класса дублирования актуален и между index.html и screens/*.js,
и между самими screens/*.js файлами — проверяем все вместе.
"""
from __future__ import annotations

import glob
import os
import re
from collections import Counter

_MINI_APP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mini_app"
)


def _mini_app_js_sources() -> dict[str, str]:
    sources = {}
    index_path = os.path.join(_MINI_APP_DIR, "index.html")
    with open(index_path, encoding="utf-8") as f:
        sources["index.html"] = f.read()
    for path in sorted(glob.glob(os.path.join(_MINI_APP_DIR, "screens", "*.js"))):
        with open(path, encoding="utf-8") as f:
            sources[os.path.relpath(path, _MINI_APP_DIR)] = f.read()
    return sources


def test_no_duplicate_function_names_in_index_html():
    sources = _mini_app_js_sources()
    # объявления вида `function name(` и `async function name(` — считаем
    # по ВСЕМ файлам вместе (общий глобальный scope classic-скриптов).
    all_names: list[str] = []
    for src in sources.values():
        all_names.extend(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", src))
    dups = {n: c for n, c in Counter(all_names).items() if c > 1}
    assert not dups, (
        "дубли function-имён в mini_app (index.html + screens/*.js) — второе "
        f"определение молча затеняет первое: {dups}"
    )
