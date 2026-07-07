"""Регрессия: в mini_app/index.html не должно быть дублей имён функций.

Две `function X` в одном скоупе → вторая молча затеняет первую, и все вызовы
уходят в неё. Так `toggleFunnel` (обычные Воронки) была сломана — её затеняла
одноимённая функция авто-воронок (бьющая в /auto_funnel вместо /funnel).
"""
from __future__ import annotations

import os
import re
from collections import Counter


def test_no_duplicate_function_names_in_index_html():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "mini_app",
        "index.html",
    )
    with open(path, encoding="utf-8") as f:
        html = f.read()
    # объявления вида `function name(` и `async function name(`
    names = re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", html)
    dups = {n: c for n, c in Counter(names).items() if c > 1}
    assert not dups, (
        f"дубли function-имён в index.html (второе определение молча затеняет первое): {dups}"
    )
