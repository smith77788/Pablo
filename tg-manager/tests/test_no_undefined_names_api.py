"""Гейт: в mini_app_api нет обращений к неопределённым именам.

Найдено при работе над паритетом: `log_exc_swallow` использовался в обработчике
ошибки (`check_proxy` → запись результата проверки прокси), но в этом модуле
НЕ импортирован. Синтаксис валиден, тесты зелёные, импорт модуля проходит — а в
рантайме, ровно когда что-то пошло не так, сам обработчик падал бы NameError,
превращая штатный сбой записи в 500 и теряя уже выполненную проверку.

Коварство класса: ломается только редкий путь (except-ветка), поэтому обычные
проверки его не видят. Здесь имена в теле функций сверяются с тем, что реально
доступно: builtins + модульные импорты/определения + локальные имена функции
(включая локальные `import x`, аргументы, присваивания, comprehension-переменные).
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"


def _module_scope(tree: ast.AST) -> set[str]:
    # Модульные дандеры доступны всегда, в AST-импортах их нет.
    names = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
    return names


def _bound_names(fn: ast.AST) -> set[str]:
    """Всё, что связывается внутри функции (и вложенных в неё)."""
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            args = getattr(n, "args", None)
            if args is not None:
                for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)):
                    out.add(a.arg)
                if args.vararg:
                    out.add(args.vararg.arg)
                if args.kwarg:
                    out.add(args.kwarg.arg)
        elif isinstance(n, ast.Lambda):
            # параметры lambda — тоже связанные имена (key=lambda o: o[...])
            for a in (list(n.args.args) + list(n.args.posonlyargs)
                      + list(n.args.kwonlyargs)):
                out.add(a.arg)
            if n.args.vararg:
                out.add(n.args.vararg.arg)
            if n.args.kwarg:
                out.add(n.args.kwarg.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global):
            out.update(n.names)
    return out


def test_no_undefined_names_in_mini_app_api():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_names = _module_scope(tree)

    # Верхнеуровневые функции (включая фабрику, внутри которой живут хендлеры)
    problems: list[str] = []
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        available = module_names | _bound_names(top)
        for n in ast.walk(top):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in available:
                    problems.append(f"{n.id} (строка {n.lineno})")

    uniq = sorted(set(problems))
    assert not uniq, (
        "обращение к неопределённому имени — в рантайме NameError, часто именно "
        "в except-ветке, где ошибка уже случилась:\n  " + "\n  ".join(uniq)
    )
