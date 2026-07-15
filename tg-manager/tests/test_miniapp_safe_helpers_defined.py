"""Регресс: каждый `_safe_*`-хелпер, ВЫЗЫВАЕМЫЙ в mini_app_api, обязан быть в нём
ОПРЕДЕЛЁН.

Баг (2026-07-12, со скринов пользователя): `_safe_fetchval` вызывался 7 раз, но
определён был только в op_worker (не импортирован сюда) → NameError «name
'_safe_fetchval' is not defined» валил в 500 эндпоинты Дашборда метрик и
Аудитории (графики висели «Загрузка…»). Определил `_safe_fetchval` рядом с
`_safe_fetch/_safe_fetchrow/_safe_count`.
"""
from __future__ import annotations

import os
import re

_API = os.path.join(os.path.dirname(__file__), "..", "services", "mini_app_api.py")


def test_all_called_safe_helpers_are_defined():
    src = open(_API, encoding="utf-8").read()
    called = set(re.findall(r"\b(_safe_\w+)\s*\(", src))
    defined = set(re.findall(r"(?:async\s+)?def\s+(_safe_\w+)\s*\(", src))
    missing = called - defined
    assert not missing, (
        f"_safe_*-хелперы вызываются, но не определены в mini_app_api "
        f"(NameError в рантайме → 500): {sorted(missing)}"
    )


def test_safe_fetchval_defined():
    src = open(_API, encoding="utf-8").read()
    assert re.search(r"async def _safe_fetchval\s*\(", src), (
        "_safe_fetchval должен быть определён (использовался, но был undefined)"
    )
