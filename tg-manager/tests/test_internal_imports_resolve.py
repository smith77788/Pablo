"""Каждый внутренний `from X import Y` обязан разрешаться (находка аудита №5).

ЗАЧЕМ. Импорты внутри функций — почти половина всех импортов в проекте, и это
осознанный приём: он разрывает циклы и ускоряет старт. Но у него есть цена,
которую платит пользователь: сломанный импорт не виден ни при старте, ни в
обычном прогоне тестов. Он взрывается ровно в тот момент, когда человек нажал
кнопку, — и превращается в 500 с текстом «cannot import name».

Именно так и было найдено: три маршрута мини-аппа (пауза, возобновление и
удаление воркфлоу) звали `pause_workflow`, `resume_workflow`, `delete_workflow`,
которых в `services.workflow_engine` не существовало вовсе. Маршруты
зарегистрированы, кнопки есть, ответ — всегда 500.

Проверка статическая: имена берутся из AST модуля-цели, ничего не импортируется
(импорт половины модулей тянет telethon/aiogram и лезет в сеть). Внешние
библиотеки не проверяются — только модули этого репозитория.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _module_name(path: str) -> str:
    rel = os.path.relpath(path, ROOT).replace(os.sep, ".")
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith(".__init__"):
        rel = rel[: -len(".__init__")]
    return rel


def _exported_names(tree: ast.Module) -> tuple[set[str], bool]:
    """Имена верхнего уровня модуля и признак `import *` (тогда не судим).

    Обходим также тела `if`/`try` — условные и защищённые импорты объявляют
    вполне настоящие имена, и считать их отсутствующими значило бы завалить
    тест ложными срабатываниями.
    """
    names: set[str] = set()
    star = False
    stack: list = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star = True
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.If, ast.Try)):
            stack.extend(node.body)
            stack.extend(getattr(node, "orelse", []))
            stack.extend(getattr(node, "handlers", []))
            stack.extend(getattr(node, "finalbody", []))
        elif isinstance(node, ast.ExceptHandler):
            stack.extend(node.body)
    return names, star


def _repo_modules() -> dict[str, tuple[set[str], bool]]:
    out: dict[str, tuple[set[str], bool]] = {}
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
            except SyntaxError:
                continue
            out[_module_name(path)] = _exported_names(tree)
    return out


def _unresolved() -> list[str]:
    mods = _repo_modules()
    bad: list[str] = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.ImportFrom)
                        and node.module and node.level == 0):
                    continue
                target = mods.get(node.module)
                if target is None:
                    continue          # внешняя библиотека — не наше дело
                names, star = target
                if star:
                    continue          # `import *` скрывает состав модуля
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if alias.name in names:
                        continue
                    if f"{node.module}.{alias.name}" in mods:
                        continue      # импортируется подмодуль пакета
                    bad.append(
                        f"{rel}:{node.lineno}: from {node.module} import {alias.name}")
    return sorted(bad)


def test_no_unresolved_internal_imports():
    bad = _unresolved()
    assert not bad, (
        "импорт ссылается на то, чего в модуле нет:\n  " + "\n  ".join(bad)
        + "\n\nЕсли это импорт внутри функции, он взорвётся не при старте, а у "
          "пользователя — 500 «cannot import name» ровно на нажатие кнопки."
    )


def test_detector_finds_a_planted_break(tmp_path):
    """Детектор, который ничего не находит, — зелёный и бесполезный."""
    mods = {"pkg.mod": ({"real", "other"}, False)}
    tree = ast.parse("def f():\n    from pkg.mod import missing\n")
    seen = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in mods:
            names, _star = mods[node.module]
            seen += [a.name for a in node.names if a.name not in names]
    assert seen == ["missing"]


def test_detector_sees_real_modules():
    """Карта модулей действительно построена — иначе проверять нечего."""
    mods = _repo_modules()
    assert "services.workflow_engine" in mods
    names, _star = mods["services.workflow_engine"]
    # Ровно те три функции, из-за отсутствия которых раздел «Воркфлоу» отвечал 500.
    assert {"pause_workflow", "resume_workflow", "delete_workflow"} <= names
