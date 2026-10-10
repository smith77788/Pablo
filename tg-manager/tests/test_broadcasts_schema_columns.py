"""Регресс «несуществующая колонка»: колонки broadcasts, которые КОД ЧИТАЕТ явным
`SELECT b.<col>` без fallback, обязаны существовать в схеме.

Баг (2026-07-12): broadcasts.silent писалась (с fallback на UndefinedColumnError)
и читалась, но колонки в схеме не было. Чтение без fallback —
broadcaster.get_broadcast_analytics `SELECT b.buttons, b.silent …` — падало на
отсутствующей колонке → эндпоинт аналитики рассылки всегда 500. Плюс фича «тихая
рассылка» не работала (значение терялось при записи). Добавлено schema_v155.

Тест строит множество колонок broadcasts из ВСЕХ schema*.sql (CREATE + ALTER ADD
COLUMN) и проверяет, что колонки, читаемые в broadcaster.get_broadcast_analytics,
там есть. Ловит будущие рассинхроны код↔схема того же класса.
"""
from __future__ import annotations

import glob
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _broadcasts_columns() -> set[str]:
    cols: set[str] = set()
    for f in sorted(glob.glob(os.path.join(_ROOT, "schema*.sql"))):
        s = open(f, encoding="utf-8", errors="replace").read()
        for m in re.finditer(
            r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+broadcasts\s*\((.*?)\n\)\s*;",
            s, re.DOTALL | re.I,
        ):
            for line in m.group(1).split("\n"):
                line = line.strip().rstrip(",")
                mm = re.match(r'"?([a-z_][a-z0-9_]*)"?\s+', line, re.I)
                if mm and mm.group(1).upper() not in (
                    "PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK", "KEY",
                ):
                    cols.add(mm.group(1).lower())
        for m in re.finditer(
            r'ALTER TABLE\s+broadcasts\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+"?([a-z_][a-z0-9_]*)"?',
            s, re.I,
        ):
            cols.add(m.group(1).lower())
    return cols


def test_silent_column_declared():
    assert "silent" in _broadcasts_columns(), (
        "broadcasts.silent должна быть объявлена (её читает get_broadcast_analytics без fallback)"
    )


def test_analytics_read_columns_exist_in_schema():
    """Все b.<col>, читаемые в get_broadcast_analytics, есть в схеме broadcasts."""
    cols = _broadcasts_columns()
    bc = open(os.path.join(_ROOT, "services", "broadcaster.py"), encoding="utf-8").read()
    m = re.search(r"async def get_broadcast_analytics\(.*?FROM\s+broadcasts\s+b\b", bc, re.DOTALL)
    assert m, "get_broadcast_analytics SELECT not found"
    read_cols = set(re.findall(r"\bb\.([a-z_][a-z0-9_]*)", m.group(0)))
    missing = read_cols - cols
    assert not missing, (
        f"get_broadcast_analytics читает колонки broadcasts, которых нет в схеме: {sorted(missing)}"
    )
