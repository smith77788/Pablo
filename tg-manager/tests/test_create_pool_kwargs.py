"""Регрессия: create_pool() не должен передавать asyncpg невалидные kwargs.

Коммит 85b72688 добавил keepalive=30 в asyncpg.create_pool(), а asyncpg
0.29.0 такой аргумент не принимает → TypeError в первой же строке main() →
бот не поднимался вообще (Railway исчерпывал 10 рестартов). Этот тест
статически извлекает kwargs из вызова asyncpg.create_pool в db.py и сверяет
их с реально допустимыми параметрами asyncpg — ловит невалидный kwarg до
деплоя, без живой БД.
"""
from __future__ import annotations

import ast
import inspect
import os

import asyncpg


def _create_pool_kwargs() -> set[str]:
    """Достаём имена kwargs из вызова asyncpg.create_pool(...) в db.py."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "database",
        "db.py",
    )
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            # ищем asyncpg.create_pool(...)
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "create_pool"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "asyncpg"
            ):
                return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError("вызов asyncpg.create_pool не найден в db.py")


def _allowed_kwargs() -> set[str]:
    """Допустимые kwargs = параметры create_pool + connect (пул проксирует connect)."""
    allowed: set[str] = set()
    for fn in (asyncpg.create_pool, asyncpg.connect):
        try:
            allowed |= set(inspect.signature(fn).parameters.keys())
        except (ValueError, TypeError):
            pass
    return allowed


def test_create_pool_uses_only_valid_asyncpg_kwargs():
    used = _create_pool_kwargs()
    allowed = _allowed_kwargs()
    invalid = used - allowed
    assert not invalid, (
        f"create_pool передаёт asyncpg неизвестные kwargs {invalid} — "
        f"это уронит запуск бота с TypeError. Допустимые: {sorted(allowed)}"
    )


def test_keepalive_not_passed():
    # keepalive конкретно валил прод — фиксируем явно, чтобы не вернулся.
    assert "keepalive" not in _create_pool_kwargs(), (
        "keepalive снова в create_pool — asyncpg 0.29 его не принимает, бот не стартует"
    )
