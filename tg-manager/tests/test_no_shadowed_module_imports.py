"""Ратчет: локальный import не должен тенить модульное имя.

Класс багов, который этот тест закрывает.

В Python любое связывание имени внутри функции (в том числе `import x`) делает
это имя ЛОКАЛЬНЫМ для ВСЕГО тела функции. Если такой import стоит под условием,
то на всех остальных ветках обращение к имени даёт `UnboundLocalError` — даже
когда то же самое имя импортировано на уровне модуля.

Так были сломаны массовые операции Mini App: `from services import
operation_bus as _obus` лежал внутри `if op == "check":`, а использовался ещё и
в ветках `leave_all` и профильных операций, где импорт не выполнялся. Каждый
такой вызов падал в `except Exception` и возвращал пользователю 500
«Failed to …» без единого намёка на причину.

`UnboundLocalError` — подкласс `NameError`, а не `TypeError`/`ValueError`,
поэтому локальные `except (TypeError, ValueError)` его не ловят: ошибка всегда
доезжает до общего обработчика и превращается в 500.

Проверяем два инварианта:

1. Нет локального import'а, полностью идентичного модульному, — такой import
   ничего не добавляет, но создаёт ровно эту ловушку.
2. Нет использования модульного имени, которое локальное связывание может
   оставить несвязанным (обобщение на случай импорта под другим алиасом).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Каталоги проекта, за которыми следим. Тесты включены намеренно: ловушка
# одинаково ломает и тестовые хелперы.
WATCHED = ("services", "bot", "database", "tests", "infra", "codex_bridge")

COND_NODES = (
    ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
    ast.ExceptHandler,
)


def _py_files() -> list[Path]:
    files: list[Path] = []
    for sub in WATCHED:
        d = ROOT / sub
        if d.is_dir():
            files.extend(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    files.extend(p for p in ROOT.glob("*.py"))
    return sorted(files)


def _parse(path: Path) -> ast.Module | None:
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # не наша забота — ловит compileall
        return None
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]
    return tree


def _alias_key(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> tuple:
    if isinstance(node, ast.ImportFrom):
        return ("from", node.module or "", node.level, alias.name, alias.asname)
    return ("import", None, 0, alias.name, alias.asname)


def _module_level(tree: ast.Module) -> tuple[set[tuple], set[str]]:
    """Ключи и имена импортов, выполняемых безусловно при импорте модуля."""
    keys: set[tuple] = set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                keys.add(_alias_key(node, alias))
                names.add((alias.asname or alias.name).split(".")[0])
    return keys, names


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _own_nodes(fn) -> set[int]:
    """id узлов функции без тел вложенных функций (у них своя область)."""
    nested: set[int] = set()
    for node in ast.walk(fn):
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested.update(id(x) for x in ast.walk(node))
    return {id(n) for n in ast.walk(fn)} - nested


def test_no_local_import_duplicates_module_import():
    """Локальный import, дословно повторяющий модульный, запрещён."""
    offenders: list[str] = []
    for path in _py_files():
        tree = _parse(path)
        if tree is None:
            continue
        keys, _ = _module_level(tree)
        if not keys:
            continue
        top = {id(n) for n in tree.body}
        for fn in _functions(tree):
            own = _own_nodes(fn)
            for node in ast.walk(fn):
                if id(node) not in own or id(node) in top:
                    continue
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                dupes = [a for a in node.names if _alias_key(node, a) in keys]
                if dupes:
                    shown = ", ".join((a.asname or a.name) for a in dupes)
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} "
                        f"{fn.name}() — локальный import {shown} дублирует модульный")
    assert not offenders, (
        "Локальный import дублирует модульный и делает имя локальным на всю "
        "функцию (UnboundLocalError на ветках без него). Уберите локальный:\n  "
        + "\n  ".join(offenders[:40])
    )


def _block_path(node, root) -> list[tuple[int, int]] | None:
    """Путь от тела функции до узла: список (id списка-блока, индекс в нём)."""
    chain: list[tuple[int, int]] = []
    cur = node
    while cur is not root:
        parent = getattr(cur, "parent", None)
        if parent is None:
            return None
        for _field, value in ast.iter_fields(parent):
            if isinstance(value, list) and cur in value:
                chain.append((id(value), value.index(cur)))
                break
        else:
            return None
        cur = parent
    chain.reverse()
    return chain


def _dominates(bind_path, use_path) -> bool:
    """bind гарантированно выполнится до use (лежит выше по тому же пути)."""
    for i, (block, idx) in enumerate(bind_path):
        if i >= len(use_path) or use_path[i][0] != block:
            return False
        if i == len(bind_path) - 1:
            return idx < use_path[i][1]
        if idx != use_path[i][1]:
            return False
    return False


def test_no_use_of_shadowed_module_name_without_binding():
    """Имя, затенённое локальным import'ом, не читается на ветках без него."""
    offenders: list[str] = []
    for path in _py_files():
        tree = _parse(path)
        if tree is None:
            continue
        _keys, mod_names = _module_level(tree)
        if not mod_names:
            continue
        for fn in _functions(tree):
            own = _own_nodes(fn)
            binds: dict[str, list[ast.AST]] = {}
            uses: dict[str, list[ast.Name]] = {}
            for node in ast.walk(fn):
                if id(node) not in own:
                    continue
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        name = (alias.asname or alias.name).split(".")[0]
                        if name in mod_names:
                            binds.setdefault(name, []).append(node)
                elif (isinstance(node, ast.Name)
                      and isinstance(node.ctx, ast.Load)
                      and node.id in mod_names):
                    uses.setdefault(node.id, []).append(node)
            for name, bind_nodes in binds.items():
                bind_paths = [p for p in (_block_path(b, fn) for b in bind_nodes)
                              if p is not None]
                for use in uses.get(name, []):
                    use_path = _block_path(use, fn)
                    if use_path is None:
                        continue
                    if any(_dominates(bp, use_path) for bp in bind_paths):
                        continue
                    lines = sorted(b.lineno for b in bind_nodes)
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{use.lineno} {fn.name}() — "
                        f"'{name}' может быть не связано (локальный import: {lines})")
    assert not offenders, (
        "Чтение имени, которое локальный import затенил, но мог не связать "
        "(UnboundLocalError в рантайме):\n  " + "\n  ".join(offenders[:40])
    )
