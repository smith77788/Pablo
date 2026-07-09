"""UX-консистентность: часто используемый CSS-класс обязан быть определён в
<style>. Иначе компонент рендерится без оформления и выглядит иначе на разных
экранах (немая несогласованность).

Реальный носитель бага: .kpi-card использовался ~77 раз в *Stats-контейнерах
модулей, но НЕ был определён → статы на ~15 экранах рендерились без фона/
паддинга/радиуса, тогда как на экране аккаунтов (.acc-kpi-card) и в .kpi-bar
(.kpi-col) карточки были оформлены. Плюс компонент .op-progress-* (прогресс
операций на дашборде).
"""
from __future__ import annotations

import os
import re

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")

# Классы-хуки только для JS-селекторов (querySelectorAll) — CSS не требуется.
_SELECTOR_ONLY = {"color-pick", "gp-acc-cb"}
_THRESHOLD = 10  # класс, используемый чаще, обязан иметь оформление


def _load():
    html = open(_HTML, encoding="utf-8").read()
    styles = "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL))
    defined = set(re.findall(r"\.([a-zA-Z_][\w-]*)", styles))
    body = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    used = {}
    for m in re.finditer(r'class="([^"]*)"', body):
        for cls in m.group(1).split():
            if "$" in cls or "{" in cls:
                continue
            used[cls] = used.get(cls, 0) + 1
    return defined, used


def test_frequent_classes_are_defined():
    defined, used = _load()
    offenders = {
        c: n for c, n in used.items()
        if n >= _THRESHOLD and c not in defined
        and not c.startswith("b-")  # бейджи b-gr/b-rd — утилитарные, определены отдельно
        and c not in _SELECTOR_ONLY
    }
    assert not offenders, (
        "Часто используемые классы без CSS-определения (немая несогласованность): "
        f"{offenders}"
    )


def test_key_components_defined():
    """Явный guard на конкретные компоненты, которые чинились."""
    defined, _ = _load()
    for cls in ("kpi-card", "op-progress-item", "op-progress-header",
                "op-progress-bar", "kpi-col", "acc-kpi-card"):
        assert cls in defined, f".{cls} должен быть определён в CSS"
