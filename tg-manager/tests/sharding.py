"""Деление набора тестов на части для параллельных прогонов CI.

ЗАЧЕМ. Набор (~12 тыс. тестов, около 25 минут чистого времени) шёл одним
прогоном и уже однажды молча перерос предел времени работы CI: GitHub
помечает убитую по таймауту работу как «отменено», и код уезжал в прод без
проверки (см. комментарий к timeout-minutes в .github/workflows/tests.yml).
Части идут на отдельных раннерах, у каждой своя база Postgres, поэтому
изоляция ровно та же, что у одного прогона, а время — в N раз меньше.

КАК ДЕЛИТСЯ. Единица — ФАЙЛ, а не тест: фикстуры уровня модуля (поднятая
база, подпроцесс main.py) должны жить в одной части. Файлы раскладываются
жадно по весу — от тяжёлых к лёгким, каждый в самую лёгкую на тот момент
часть. Вес — секунды файла из `tests/shard_weights.json`; у нового файла веса
нет, и он считается средним по набору. Раскладка зависит только от списка
файлов и весов, поэтому у всех частей она одна и та же, и каждый файл попадает
ровно в одну часть (это проверяет tests/test_sharding.py).

Файл, не попавший в свою часть, НЕ ИМПОРТИРУЕТСЯ (`pytest_ignore_collect` в
conftest): сбор набора стоит ~50 с, и делить надо его тоже.

Веса устаревают — это стоит только неровности частей, а не пропуска тестов.
Обновить: `python -m tests.sharding --update <junit.xml>`.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WEIGHTS = os.path.join(ROOT, "shard_weights.json")
ENV = "INFRAGRAM_TEST_SHARD"


def parse_shard(value: str | None) -> tuple[int, int] | None:
    """`"2/4"` → (2, 4). Пусто → None (весь набор). Мусор — ошибка, а не
    молчаливый полный прогон: опечатка в CI иначе гоняла бы весь набор в
    каждой части, и никто бы не заметил."""
    if not value or not value.strip():
        return None
    try:
        k, n = (int(x) for x in value.strip().split("/"))
    except ValueError:
        raise ValueError(f"{ENV}={value!r}: нужен вид k/n, например 2/4") from None
    if n < 1 or not 1 <= k <= n:
        raise ValueError(f"{ENV}={value!r}: нужно 1 <= k <= n")
    return k, n


def load_weights(path: str = WEIGHTS) -> dict[str, float]:
    try:
        with open(path, encoding="utf-8") as fh:
            return {str(k): float(v) for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        return {}


def list_test_files(root: str = ROOT) -> list[str]:
    return sorted(f for f in os.listdir(root)
                  if f.startswith("test_") and f.endswith(".py"))


def assign(files: list[str], n: int, weights: dict[str, float]) -> dict[str, int]:
    """Файл → номер части (1..n). Жадно: тяжёлые первыми, в самую лёгкую."""
    known = [weights[f] for f in files if f in weights]
    default = (sum(known) / len(known)) if known else 1.0
    # Ключ сортировки полностью детерминирован: вес, затем имя.
    order = sorted(files, key=lambda f: (-weights.get(f, default), f))
    load = [0.0] * n
    out: dict[str, int] = {}
    for f in order:
        i = min(range(n), key=lambda j: (load[j], j))
        out[f] = i + 1
        # Нулевой вес (файл из одних быстрых тестов) всё равно стоит сбора.
        load[i] += max(weights.get(f, default), 0.05)
    return out


def files_for(k: int, n: int, root: str = ROOT) -> set[str]:
    plan = assign(list_test_files(root), n, load_weights())
    return {f for f, part in plan.items() if part == k}


def _update(junit_paths: list[str]) -> int:
    """Пересчитать веса по junit-отчётам прогонов (любого числа частей)."""
    import collections
    import xml.etree.ElementTree as ET

    per: dict[str, float] = collections.Counter()
    for p in junit_paths:
        for tc in ET.parse(p).getroot().iter("testcase"):
            parts = tc.attrib.get("classname", "").split(".")
            name = (parts[1] if len(parts) > 1 else parts[0]) + ".py"
            per[name] += float(tc.attrib.get("time") or 0)
    alive = set(list_test_files())
    data = {k: round(v, 1) for k, v in sorted(per.items()) if k in alive}
    with open(WEIGHTS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=0)
        fh.write("\n")
    print(f"веса: {len(data)} файлов, {round(sum(data.values()))} с")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--update":
        raise SystemExit(_update(sys.argv[2:]))
    sys.exit("использование: python -m tests.sharding --update <junit.xml> [...]")
