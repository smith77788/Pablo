"""Инструменты тестов в CI — точных версий и из одного места.

CI ставил `pytest pytest-asyncio ruff` без версий, то есть самые свежие на
каждом прогоне. Новый выпуск любого из них (смена умолчаний pytest-asyncio,
новое правило ruff) красил весь набор деплойной ветки без единой правки кода.
Теперь CI ставит их из requirements-dev.txt, где версии точные.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, "..", ".github", "workflows", "tests.yml")
DEV = os.path.join(ROOT, "requirements-dev.txt")


def _dev_reqs() -> dict[str, str]:
    out = {}
    for line in open(DEV, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, ver = line.partition("==")
            out[name.strip().lower()] = ver.strip()
    return out


def test_dev_tools_are_pinned_exactly():
    reqs = _dev_reqs()
    for tool in ("pytest", "pytest-asyncio", "ruff"):
        assert reqs.get(tool), f"{tool}: нужна точная версия (==) в requirements-dev.txt"


def test_ci_installs_dev_tools_from_the_pinned_file():
    wf = open(WORKFLOW, encoding="utf-8").read()
    step = wf.split("- name: Install deps", 1)[1].split("- name:", 1)[0]
    assert "-r requirements-dev.txt" in step
    # Комментарии шага упоминают инструменты словами — проверяем только команды.
    step = "\n".join(ln for ln in step.splitlines() if not ln.strip().startswith("#"))
    for tool in ("pytest", "pytest-asyncio", "ruff"):
        assert not re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", step.replace(
            "requirements-dev.txt", "")), f"{tool} ставится в CI мимо закреплённой версии"
