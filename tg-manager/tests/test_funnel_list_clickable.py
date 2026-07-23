"""Регресс: воронки в обзоре рассылок кликабельны → открывают детальный экран.

Была дыра: секция «Воронки» на экране рассылок рендерилась `renderFuns` как
НЕкликабельный список (`cursor:default`), тогда как тот же список воронок под
ботами (`bfList`) вёл в `openFunnelDetail`. Пользователь, тапнувший воронку
здесь, упирался в тупик — сценарий не доводился до результата. Оба списка
одной сущности должны вести в одну деталь.
"""
from __future__ import annotations

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"


def _render_funs_src() -> str:
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"function renderFuns\(items\)\s*\{.*?\n\}", html, re.DOTALL)
    assert m, "renderFuns не найдена"
    return m.group(0)


def test_funnel_rows_open_detail():
    src = _render_funs_src()
    assert "openFunnelDetail(" in src, (
        "строки воронок должны открывать деталь (openFunnelDetail) — иначе тупик"
    )
    assert "cursor:default" not in src, (
        "renderFuns не должна помечать строки как некликабельные"
    )
    assert "class=\"li tap\"" in src, "строки воронок должны быть кликабельны (li tap)"
