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


def test_the_fingerprint_moves_with_the_file_list():
    """Отпечаток обязан меняться от нового файла и не зависеть от порядка.

    Иначе он бесполезен ровно там, где нужен: два дерева, различающиеся одним
    файлом тестов, дают РАЗНЫЕ раскладки под одним номером части."""
    files = sharding.list_test_files()
    a = sharding.layout_id(files)
    assert re.fullmatch(r"[0-9a-f]{12}", a), a
    assert sharding.layout_id(list(reversed(files))) == a, "порядок списка влиять не должен"
    b = sharding.layout_id(files + ["test_novyy_fayl_testov.py"])
    assert b != a, ("отпечаток не меняется от нового файла тестов — по нему нельзя "
                    "понять, что раскладка другая, и чужое «часть 2 красная» "
                    "опять нечем воспроизвести")


def test_the_part_line_is_visible_in_the_quiet_run():
    """Строку про часть видно в ТОМ прогоне, которым ходит CI.

    В pytest.ini стоит `addopts = -q`, и CI зовёт pytest тоже с `-q`. В тихом
    режиме вывод pytest_report_header не показывается вовсе, поэтому первая
    редакция этой строки была не видна нигде, кроме подробного прогона, —
    мерка, которой не видно, не мерка. Проверяем настоящим подпроцессом.
    """
    import subprocess
    import sys

    env = {**os.environ, sharding.ENV: "2/4"}
    env.pop("INFRAGRAM_TEST_DSN", None)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_sharding.py",
         "--collect-only", "-q", "-p", "no:randomly"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300).stdout
    assert "отпечаток раскладки" in out, (
        "в тихом прогоне (-q, как в CI) не видно, какая это часть и какова "
        f"раскладка:\n{out[:600]}")
    assert "часть набора 2/4" in out, out[:600]


def test_the_part_line_counts_files_in_russian():
    from tests import conftest as cf

    assert (cf._files_word(1), cf._files_word(373), cf._files_word(375),
            cf._files_word(11), cf._files_word(14)) == (
        "файл", "файла", "файлов", "файлов", "файлов")


def test_the_run_header_names_the_part_and_the_layout(monkeypatch):
    """Прогон части печатает, какая это часть и какова раскладка."""
    from tests import conftest as cf

    assert hasattr(cf, "pytest_report_header"), (
        "шапка прогона не называет часть: красную часть нечем воспроизвести")
    saved = cf._SHARD_FILES
    try:
        monkeypatch.setenv(sharding.ENV, "2/4")
        cf.pytest_configure(type("Cfg", (), {})())
        line = cf.pytest_report_header(None)
        assert line and "2/4" in line, line
        assert sharding.layout_id() in line, line
        assert str(len(sharding.files_for(2, 4))) in line, line
        monkeypatch.delenv(sharding.ENV)
        cf.pytest_configure(type("Cfg", (), {})())
        assert cf.pytest_report_header(None) is None, "без переменной шапки быть не должно"
    finally:
        cf._SHARD_FILES = saved
