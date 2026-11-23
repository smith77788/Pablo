"""Доска сделок: быстрый текстовый отчёт по воронке в буфер — как «📋 Отчёт» в
«Пересечениях аудитории» (eco_overlaps) и «Плане роста» (growth_plan). По
стадиям: число сделок и суммы по валютам; плюс итоги «в работе» и «выиграно».
Суммы между валютами не складываются (CLAUDE.md: рубли с долларами — красивое
неверное число).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = (ROOT / "mini_app/screens/deals_board.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", DB)
    assert m, name
    return m.group(0)


def test_board_has_copy_report_button():
    render = _fn("_dkRender")
    assert "dkCopyReport()" in render
    assert "📋 Отчёт" in render


def test_report_text_builds_from_columns_by_currency():
    body = _fn("dkReportText")
    assert "_DK.columns" in body or "cols = _DK.columns" in body
    assert "by_currency" in body
    assert "Выиграно" in body
    assert "В работе" in body


def test_report_does_not_sum_across_currencies():
    body = _fn("dkReportText")
    # суммы копятся в словарь по коду валюты, а не в один общий итог
    assert "openCur[p[0]]" in body


def test_copy_uses_clipboard_helper():
    body = _fn("dkCopyReport")
    assert "copyToClipboard(" in body
