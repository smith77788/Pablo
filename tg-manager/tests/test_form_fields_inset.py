"""Поля форм упирались в край экрана — «кривые окна в разделах».

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «кривые окна в разделах».

ЗАМЕР В БРАУЗЕРЕ (Playwright, 390px, все экраны развёрнуты, вложенные
`display:none` принудительно показаны): из 14 экранов с формами у **11** рамка
ввода начиналась на x=0 — вплотную к границе экрана, без единого пикселя отступа.
Нормальный отступ был только у трёх. Канонический отступ приложения — 16px, как у
заголовков секций (`.sec{padding:16px 16px 6px}`).

Причина оказалась не одна:
  * 12 обёрток формы имели `padding:8px 0` — вертикальный отступ есть,
    горизонтального нет;
  * у «Сеттера профилей» обёртки полей (`#setterNameFields` и соседи) не имели
    padding вовсе;
  * часть полей лежала прямо в теле экрана, без обёртки.

Тест статический (Playwright в pytest не поднимаем), поэтому стережёт ПРИЧИНЫ:
отсутствие `padding:8px 0` у обёрток формы и наличие правила для полей без
обёртки. Полный геометрический замер воспроизводится харнессом рендера.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

_DIV = re.compile(r"<(/?)div\b", re.I)


def _screen_bodies() -> dict[str, str]:
    out = {}
    for m in re.finditer(r'<div class="screen" id="(s-[a-z0-9_-]+)"', HTML):
        sid, start = m.group(1), m.start()
        depth, end = 0, start
        for t in _DIV.finditer(HTML, start):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                end = t.end()
                break
        out[sid] = HTML[start:end]
    return out


def test_no_form_wrapper_without_horizontal_padding():
    """`padding:8px 0` на обёртке формы = поля вплотную к краю экрана."""
    bad = []
    for sid, body in _screen_bodies().items():
        if 'class="field"' not in body:
            continue
        if 'style="padding:8px 0"' in body:
            bad.append(sid)
    assert not bad, (
        "обёртка формы без горизонтального отступа — поля упрутся в край: "
        + ", ".join(bad)
    )


def test_bare_fields_get_the_canonical_inset():
    """Поле, лежащее прямо в теле экрана, тоже должно быть отбито от края."""
    css = "\n".join(m.group(1) for m in re.finditer(r"<style>(.*?)</style>", HTML, re.DOTALL))
    m = re.search(r"\.sub-sb\s*>\s*\.field\{([^}]*)\}", css)
    assert m, "нет правила для полей без обёртки"
    assert "padding-left:16px" in m.group(1) and "padding-right:16px" in m.group(1), (
        "отступ должен совпадать с каноническим (16px, как у .sec)"
    )


def test_profile_setter_wrappers_are_inset():
    """Сеттер профилей — единственный экран, где обёртки полей не имели padding
    вовсе; без него правило для «голых» полей его не спасает (поля вложены)."""
    for wid in ("setterNameFields", "setterAvatarFields", "setter2faFields"):
        m = re.search(rf'<div id="{wid}"([^>]*)>', HTML)
        assert m, f"обёртка {wid} исчезла"
        assert "padding:0 16px" in m.group(1), (
            f"{wid}: поля внутри упрутся в край экрана"
        )


def test_canonical_inset_matches_section_headers():
    """Если канон в приложении сменят, тест должен об этом сказать, а не молча
    разойтись с ним."""
    css = "\n".join(m.group(1) for m in re.finditer(r"<style>(.*?)</style>", HTML, re.DOTALL))
    m = re.search(r"\.sec\{padding:16px 16px", css)
    assert m, (
        "канонический отступ секции изменился — выровнять по нему отступ полей"
    )
