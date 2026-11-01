"""Деление набора на части CI не теряет и не дублирует ни одного файла.

Набор в CI идёт N частями на разных раннерах (tests/sharding.py). Ошибка
раскладки — это файл, который не выполняется НИГДЕ, и зелёный CI поверх
непроверенного кода: тот самый класс «молча не выполнялось», ради которого
в CI вообще подняли Postgres. Поэтому раскладка — под тестом.
"""
from __future__ import annotations

import os
import re

import pytest

from tests import sharding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ci_shard_count() -> int:
    wf = os.path.join(ROOT, "..", ".github", "workflows", "tests.yml")
    text = open(wf, encoding="utf-8").read()
    m = re.search(r"shard:\s*\[([\d,\s]+)\]", text)
    assert m, "в tests.yml не найдена матрица частей (shard: [...])"
    parts = [int(x) for x in m.group(1).split(",") if x.strip()]
    assert parts == list(range(1, len(parts) + 1)), parts
    m2 = re.search(r"INFRAGRAM_TEST_SHARD:\s*\$\{\{\s*matrix\.shard\s*\}\}/(\d+)", text)
    assert m2, "части не передают INFRAGRAM_TEST_SHARD=<k>/<n>"
    assert int(m2.group(1)) == len(parts), "n в INFRAGRAM_TEST_SHARD ≠ числу частей"
    return len(parts)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 7])
def test_every_file_lands_in_exactly_one_part(n):
    files = sharding.list_test_files()
    parts = [sharding.files_for(k, n) for k in range(1, n + 1)]
    union = set().union(*parts)
    assert union == set(files), sorted(set(files) - union)[:5]
    assert sum(len(p) for p in parts) == len(files), "файл попал в две части"


def test_the_ci_matrix_matches_the_split():
    n = _ci_shard_count()
    files = sharding.list_test_files()
    parts = [sharding.files_for(k, n) for k in range(1, n + 1)]
    assert set().union(*parts) == set(files)


def test_parts_are_balanced_by_weight():
    """Неровные части — не потеря тестов, но потеря смысла деления: самая
    тяжёлая часть и есть время CI."""
    n = _ci_shard_count()
    w = sharding.load_weights()
    files = sharding.list_test_files()
    avg = sum(w.values()) / max(len(w), 1)
    loads = [sum(w.get(f, avg) for f in sharding.files_for(k, n)) for k in range(1, n + 1)]
    assert max(loads) <= 1.25 * (sum(loads) / n) + max(w.values(), default=0), loads


def test_new_file_without_weight_still_has_a_part():
    plan = sharding.assign(["test_a.py", "test_new.py"], 2, {"test_a.py": 10.0})
    assert set(plan) == {"test_a.py", "test_new.py"}
    assert set(plan.values()) == {1, 2}


@pytest.mark.parametrize("bad", ["0/4", "5/4", "x", "2-4", "1/0"])
def test_garbage_spec_is_an_error_not_a_full_run(bad):
    with pytest.raises(ValueError):
        sharding.parse_shard(bad)


def test_empty_spec_means_whole_suite():
    assert sharding.parse_shard("") is None
    assert sharding.parse_shard(None) is None
    assert sharding.parse_shard("2/4") == (2, 4)
