"""Валидатор миграций: все schema*.sql должны корректно разбиваться на statements
и не содержать очевидно битых конструкций. Ловит поломанные миграции ДО деплоя
(create_pool применяет их толерантно, поэтому битый файл иначе тонет в warning).
"""
from __future__ import annotations

import glob
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_FILES = sorted(glob.glob(os.path.join(_ROOT, "schema*.sql")))


def test_schema_files_exist():
    assert len(_SCHEMA_FILES) > 50  # база + сотни миграций


@pytest.mark.parametrize("path", _SCHEMA_FILES, ids=lambda p: os.path.basename(p))
def test_schema_splits_cleanly(path):
    from database.db import _split_sql_statements
    sql = open(path, encoding="utf-8").read().strip()
    if not sql:
        return
    stmts = _split_sql_statements(sql)  # не должно бросать
    assert isinstance(stmts, list)
    # каждый разбитый statement — непустой (после фильтра комментариев)
    for s in stmts:
        assert s.strip()


@pytest.mark.parametrize("path", _SCHEMA_FILES, ids=lambda p: os.path.basename(p))
def test_schema_balanced_dollar_blocks(path):
    """Число открытий/закрытий DO $$ ... $$ должно быть чётным (иначе split съедет)."""
    sql = open(path, encoding="utf-8").read()
    assert sql.count("$$") % 2 == 0, f"нечётное число $$ в {os.path.basename(path)}"
