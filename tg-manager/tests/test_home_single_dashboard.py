"""Главная: единый дашборд — счётчики один раз (лента KPI), плитки — навигация.

Жалоба: «нету единого дэшборда вместо нескольких». На «Главной» цифры показывались
ДВАЖДЫ: в верхней ленте KPI и на плитках «Быстрые действия» (Боты/Аккаунты/Каналы/
Рассылки/Операции). Фикс: плитки стали чистой навигацией (без qa-tile-val), все
счётчики живут один раз в ленте KPI.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _qa_grid():
    m = re.search(r'<div class="sec">Быстрые действия</div>(.*?)</div>\s*<!-- Часто',
                  HTML, re.DOTALL)
    assert m, "блок быстрых действий не найден"
    return m.group(1)


def test_quick_tiles_have_no_duplicate_counts():
    grid = _qa_grid()
    # плитки больше не содержат числовых значений (дублировали KPI)
    assert "qa-tile-val" not in grid, "плитки не должны дублировать счётчики KPI"
    for stray in ("qa-bots", "qa-accs", "qa-chs", "qa-bc", "qa-ops"):
        assert stray not in grid, f"{stray} — остаток дубля счётчика на плитке"


def test_tiles_still_navigate():
    grid = _qa_grid()
    # навигация сохранена
    for nav in ("goTab('bots')", "goTab('accounts')", "openChannels()", "openOps()"):
        assert nav in grid, f"плитка потеряла навигацию: {nav}"


def test_kpi_row_is_single_source_of_counts():
    # лента KPI остаётся единым источником счётчиков (аккаунты в т.ч.)
    assert "kpiScroll" in HTML
    m = re.search(r"const kpis = \[(.*?)\];", HTML, re.DOTALL)
    assert m, "массив KPI не найден"
    kpis = m.group(1)
    assert "d.accounts" in kpis and "d.bots" in kpis and "d.channels" in kpis


def test_render_no_longer_populates_tile_counts():
    # renderCards не пишет числа в плитки (иначе снова дубль)
    m = re.search(r"function renderCards\(d\)\s*\{(.*?)\nfunction renderMiniChart",
                  HTML, re.DOTALL)
    assert m, "renderCards не найден"
    body = m.group(1)
    assert "s('qa-bots'" not in body and "s('qa-accs'" not in body
