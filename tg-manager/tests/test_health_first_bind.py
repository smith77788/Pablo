"""Регрессия: $PORT биндился ПОСЛЕ create_pool (медленные миграции) →
Railway «Application failed to respond». Теперь минимальный health-сервер
занимает $PORT ДО тяжёлой инициализации, отвечает 200, освобождает порт перед
стартом реального сервера.
"""
from __future__ import annotations

import inspect
import re
import ast


def _main_src():
    with open("main.py", encoding="utf-8") as f:
        return f.read()


def test_bootstrap_helpers_exist():
    src = _main_src()
    assert "async def _start_bootstrap_health_server" in src
    assert "async def _stop_bootstrap_health_server" in src
    # отвечает 200 на любой путь
    assert 'add_route("*"' in src and "status=200" in src


def test_bootstrap_starts_before_create_pool():
    src = _main_src()
    i_start = src.index("await _start_bootstrap_health_server()")
    i_pool = src.index("pool = await create_pool()")
    i_stop = src.index("await _stop_bootstrap_health_server()")
    # старт bootstrap ДО create_pool, стоп — ПОСЛЕ (перед реальным сервером)
    assert i_start < i_pool < i_stop


def test_main_module_parses():
    ast.parse(_main_src())
