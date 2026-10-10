"""Гейт против мёртвых кнопок на JS-уровне: каждый onclick="fn(...)" определён.

Класс #4 (мёртвая кнопка): кнопка зовёт JS-функцию, которой нет → тихо падает в
консоль, для пользователя «ничего не происходит».

ВАЖНО: функции определяются и в inline-<script> index.html, И в mini_app/screens/*.js
(грузятся отдельно). Тест ОБЯЗАН сканировать оба источника — иначе даёт ложные
срабатывания (именно на этом обжёгся автор: принял за мёртвые 4 функции из screens/).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
SCREENS = "\n".join(
    p.read_text(encoding="utf-8") for p in sorted((ROOT / "mini_app" / "screens").glob("*.js"))
) if (ROOT / "mini_app" / "screens").is_dir() else ""

# Весь JS: inline-скрипты index.html + все screens/*.js.
_JS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.DOTALL)) + "\n" + SCREENS
# onclick встречаются и в HTML, и в JS-шаблонах (screens генерируют разметку строками).
_MARKUP = HTML + "\n" + SCREENS

_IGNORE = {"event", "return", "if", "for", "window", "document", "navigator",
           "tg", "void", "this", "alert", "confirm"}


def _defined() -> set[str]:
    d = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", _JS))
    d |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", _JS))
    d |= set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=", _JS))
    d |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*async", _JS))
    return d


def _onclick_fns() -> set[str]:
    # onclick="fn(...)" и onclick=`fn(...)` (в JS-шаблонах) и onclick='fn(...)'
    fns = set()
    for q in ('"', "'", "`"):
        fns |= set(re.findall(r'''onclick=''' + q + r'''\s*([A-Za-z_$][\w$]*)\s*\(''', _MARKUP))
    return fns


def test_no_dead_onclick_handlers():
    defined = _defined()
    missing = sorted(fn for fn in _onclick_fns() if fn not in defined and fn not in _IGNORE)
    assert not missing, "onclick без определения (мёртвые кнопки):\n" + "\n".join(missing)
