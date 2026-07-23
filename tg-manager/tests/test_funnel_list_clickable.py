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


def _fn_src(name: str) -> str:
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"function " + re.escape(name) + r"\(items\)\s*\{.*?\n\}", html, re.DOTALL)
    assert m, f"{name} не найдена"
    return m.group(0)


def test_funnel_rows_open_detail():
    src = _fn_src("renderFuns")
    assert "openFunnelDetail(" in src, (
        "строки воронок должны открывать деталь (openFunnelDetail) — иначе тупик"
    )
    assert "cursor:default" not in src, (
        "renderFuns не должна помечать строки как некликабельные"
    )
    assert "class=\"li tap\"" in src, "строки воронок должны быть кликабельны (li tap)"


def test_dm_campaign_overview_rows_open_manager():
    # Обзор DM-кампаний на экране рассылок тоже вёл в тупик (cursor:default),
    # хотя есть полноценный экран управления openDmCampaigns. Тап → менеджер.
    src = _fn_src("renderCmps")
    assert "openDmCampaigns()" in src, (
        "строки DM-кампаний в обзоре должны вести в менеджер — иначе тупик"
    )
    assert "cursor:default" not in src, (
        "renderCmps не должна помечать строки как некликабельные"
    )
