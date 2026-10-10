"""Регресс: таблицы Network Builder, которые читает/пишет код, объявлены в схеме.

Баг (2026-07-12, со скрина пользователя): экран «Конструктор сетей» падал
`relation "network_instances" does not exist` — network_builder.init_network_tables
создаёт таблицы, но НЕ вызывается нигде, а в schema*.sql их не было. Добавлено
schema_v156. Тест проверяет, что все network_* таблицы, на которые ссылается код,
есть в схеме.
"""
from __future__ import annotations

import glob
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _schema_tables() -> set[str]:
    tables: set[str] = set()
    for f in glob.glob(os.path.join(_ROOT, "schema*.sql")):
        s = open(f, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", s, re.I):
            tables.add(m.group(1).lower())
    return tables


def test_network_tables_declared():
    tables = _schema_tables()
    for t in ("network_templates", "network_instances", "network_nodes", "network_edges"):
        assert t in tables, f"таблица {t} (Network Builder) должна быть объявлена в schema*.sql"
