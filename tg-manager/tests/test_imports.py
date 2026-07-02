"""Smoke-тест импортов: ловит поломки старта (как незавершённый ребрендинг
InfragramChannelFSM, который ронял main.py и откатывал деплой).

Импортирует main + все хендлеры/сервисы. Ошибки импорта (ImportError, NameError,
SyntaxError) — падение теста. Telethon застаблен в conftest.
"""
from __future__ import annotations

import glob
import importlib
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _modules(pattern: str) -> list[str]:
    out = []
    for f in sorted(glob.glob(os.path.join(_ROOT, pattern))):
        if f.endswith("__init__.py"):
            continue
        rel = os.path.relpath(f, _ROOT)[:-3]
        out.append(rel.replace(os.sep, "."))
    return out


@pytest.mark.parametrize("mod", _modules("bot/handlers/*.py"))
def test_handler_imports(mod):
    importlib.import_module(mod)


@pytest.mark.parametrize("mod", _modules("services/*.py"))
def test_service_imports(mod):
    importlib.import_module(mod)


def test_main_imports():
    importlib.import_module("main")
