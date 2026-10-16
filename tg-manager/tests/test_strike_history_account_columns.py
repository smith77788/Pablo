"""Честный per-account счётчик Strike сохраняется в историю и виден во вкладке.

Раньше strike_history хранила только peer_reported — в «Истории» не было видно,
что часть флота словила флуд/бан. Добавлены колонки accounts_ok/flood/banned/
failed: миграция + defensive DDL + INSERT в _exec_strike + показ в истории.
"""
from __future__ import annotations

import inspect
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COLS = ("accounts_ok", "accounts_flood", "accounts_banned", "accounts_failed")


def test_migration_adds_columns():
    mig = open(os.path.join(ROOT, "schema_v201_strike_account_outcomes.sql"),
               encoding="utf-8").read()
    for c in _COLS:
        assert f"ADD COLUMN IF NOT EXISTS {c}" in mig, c


def test_defensive_ddl_selfheals_columns():
    m = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    for c in _COLS:
        assert f"strike_history ADD COLUMN IF NOT EXISTS {c}" in m, c


def test_exec_strike_insert_persists_columns_with_matching_placeholders():
    from services import op_worker
    src = inspect.getsource(op_worker._exec_strike)
    # найти именно INSERT INTO strike_history(...) VALUES(...)
    m = re.search(r"INSERT INTO strike_history\((.*?)\)\s*VALUES\((.*?)\)",
                  src, re.S)
    assert m, "не найден INSERT INTO strike_history"
    cols_blob, vals_blob = m.group(1), m.group(2)
    for c in _COLS:
        assert c in cols_blob, f"колонка {c} не пишется в историю"
    # число колонок == числу плейсхолдеров $N (классический баг рассинхрона)
    n_cols = len([x for x in cols_blob.split(",") if x.strip()])
    n_ph = len(set(re.findall(r"\$\d+", vals_blob)))
    assert n_cols == n_ph, f"колонок {n_cols} != плейсхолдеров {n_ph}"


def test_history_handler_reads_and_shows_breakdown():
    h = open(os.path.join(ROOT, "bot", "handlers", "strike.py"), encoding="utf-8").read()
    for c in _COLS:
        assert c in h, f"история не читает {c}"
    # строка разбивки в выводе
    assert "флуд · ⛔" in h and "бан · ⚠️" in h
