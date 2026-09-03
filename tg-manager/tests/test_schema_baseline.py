"""Baseline схемы: манифест и порядок файлов истории.

Чистая база проигрывает всю историю миграций подряд — сейчас это 194 файла.
Baseline сворачивает историю в снимок: файлы из манифеста помечаются
применёнными и не выполняются. Ошибка в манифесте молча пропустила бы
непроигранную миграцию, поэтому разбор манифеста и порядок файлов — под тестом.
"""
from __future__ import annotations

import importlib.util
import os

from database.db import parse_baseline_manifest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_tool():
    path = os.path.join(ROOT, "deploy", "scripts", "make_schema_baseline.py")
    spec = importlib.util.spec_from_file_location("make_schema_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manifest_ignores_comments_and_blanks():
    text = "# заголовок\nschema.sql\n\n  schema_v2.sql  # хвостовой комментарий\n#schema_v3.sql\n"
    assert parse_baseline_manifest(text) == {"schema.sql", "schema_v2.sql"}


def test_manifest_keeps_only_basenames():
    """В журнал миграций пишется базовое имя — путь в манифесте не должен
    порождать запись, которая ни с чем не совпадёт."""
    assert parse_baseline_manifest("database/schema_v7.sql\n") == {"schema_v7.sql"}


def test_manifest_of_empty_text_is_empty():
    assert parse_baseline_manifest("") == set()
    assert parse_baseline_manifest("# только комментарий\n") == set()


def test_tool_orders_files_like_runtime():
    """Порядок файлов у генератора обязан совпадать с порядком применения в
    рантайме: иначе --upto отрежет историю не там, где ожидает человек."""
    tool = _load_tool()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    assert names, "не нашлось ни одного schema*.sql"
    assert names[0] == "schema.sql", "базовая схема должна идти первой"
    versions = []
    for n in names[1:]:
        digits = "".join(ch for ch in n if ch.isdigit())
        if digits:
            versions.append(int(digits))
    assert versions == sorted(versions), "файлы истории должны идти по возрастанию версии"


def test_tool_never_includes_the_baseline_itself():
    tool = _load_tool()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    assert not [n for n in names if n.startswith("schema_baseline")]


def test_tool_deduplicates_by_basename():
    """Файлы схемы лежат и в корне, и в database/ — дубль по имени в манифесте
    пометил бы применённым файл, который рантайм считает одним."""
    tool = _load_tool()
    names = [os.path.basename(p) for p in tool.schema_files(ROOT)]
    assert len(names) == len(set(names))
