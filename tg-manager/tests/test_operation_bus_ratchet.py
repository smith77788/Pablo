"""Волна S (спина организма), 1A: единая нервная система — всё действие через
operation_bus. Сейчас есть 58 прямых `INSERT INTO operation_queue` в обход шины
(они не получают единых ретраев/аудита/проверки тарифа). Полная миграция —
инкрементальная, но регресс в обратную сторону недопустим.

Этот тест — храповик (ratchet): фиксирует текущий максимум обходов. Новые прямые
вставки в обход шины его ломают (заставляя автора идти через operation_bus.submit);
миграция обходов на шину — только УМЕНЬШАЕТ число, тест продолжает проходить.

Когда мигрируете обходы — уменьшайте BASELINE до нового факта (двигать вниз можно,
вверх — нет).
"""
from __future__ import annotations

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Текущий факт на 2026-07-13 (services/ + bot/, без самой шины). Двигать только
# ВНИЗ по мере миграции прямых вставок на operation_bus.submit().
BASELINE = 55

# operation_bus сам содержит эталонные вставки (реализация шины) — это не обход.
_ALLOW = {"services/operation_bus.py"}


def _count_direct_inserts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for base in ("services", "bot"):
        root = os.path.join(ROOT, base)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT)
                if rel in _ALLOW:
                    continue
                with open(path, encoding="utf-8") as f:
                    n = len(re.findall(r"INSERT INTO operation_queue", f.read()))
                if n:
                    counts[rel] = n
    return counts


def test_no_new_operation_queue_bypass():
    counts = _count_direct_inserts()
    total = sum(counts.values())
    assert total <= BASELINE, (
        f"Прямых INSERT INTO operation_queue в обход operation_bus стало {total} "
        f"(> baseline {BASELINE}). Новые операции ставить через operation_bus.submit(), "
        f"а не прямой вставкой. По файлам: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    )


def test_baseline_is_honest():
    """BASELINE не должен быть занижен ниже реального факта — иначе ratchet
    молча «зелёный» при регрессе. (Если упал ниже — обнови BASELINE вниз.)"""
    total = sum(_count_direct_inserts().values())
    assert total <= BASELINE
    # запас честности: baseline не абсурдно велик
    assert BASELINE <= 60
