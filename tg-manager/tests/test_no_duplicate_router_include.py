"""Храповик: один и тот же роутер не регистрируется в Dispatcher дважды.

Почему отдельный тест, а не «поймают обычные».

aiogram запрещает подключать Router второй раз: сеттер Router.parent_router
поднимает RuntimeError («Router is already attached to ...»). Падает это внутри
main() — то есть при СТАРТЕ процесса, ещё до dp.start_polling. Снаружи выглядит
так: контейнер собрался, платформа отрапортовала «deployment successful», а бот
не отвечает вообще ни на что и перезапускается по кругу.

Весь остальной набор тестов такую регрессию пропускает: тесты не вызывают
main() — они импортируют модули и дёргают функции. Дубль include_router для них
невидим, поэтому и нужен разбор исходника.

Так уже случилось 2026-09-04: при возвращении модуля budget_radar из чужой ветки
строка dp.include_router(budget_radar_handler.router) была добавлена дважды —
рядом с metrics_dashboard и после admin_handler. Прод лежал, пока это не нашли.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _arg_source(node: ast.AST) -> str:
    """Текст аргумента include_router — «module.router» и т.п."""
    return ast.unparse(node)


def _scopes(tree: ast.AST):
    """Модуль и каждая функция — отдельная область: в разных функциях
    одинаковый вызов не конфликтует, в одной — конфликтует."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _includes_in_scope(scope: ast.AST) -> list[tuple[str, int]]:
    """Вызовы .include_router(...) в этой области, без вложенных функций."""
    found: list[tuple[str, int]] = []
    nested = {
        n for n in ast.walk(scope)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not scope
    }
    inner: set[ast.AST] = set()
    for fn in nested:
        inner |= set(ast.walk(fn))
    for node in ast.walk(scope):
        if node in inner:
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and len(node.args) == 1
        ):
            found.append((_arg_source(node.args[0]), node.lineno))
    return found


def test_no_router_included_twice():
    offenders: list[str] = []
    for py in sorted(ROOT.rglob("*.py")):
        if "/tests/" in str(py) or "/.venv/" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for scope in _scopes(tree):
            seen: dict[str, int] = {}
            for arg, lineno in _includes_in_scope(scope):
                if arg in seen:
                    offenders.append(
                        f"{py.relative_to(ROOT)}: include_router({arg}) "
                        f"— строки {seen[arg]} и {lineno}"
                    )
                else:
                    seen[arg] = lineno

    assert not offenders, (
        "Роутер подключается к Dispatcher дважды — aiogram поднимет "
        "RuntimeError('Router is already attached') при старте процесса, "
        "и бот не поднимется вообще:\n  " + "\n  ".join(offenders)
    )


def test_aiogram_still_rejects_double_include():
    """Проверка допущения: если aiogram однажды разрешит повтор, тест выше
    станет бессмысленным строгачом — пусть об этом скажут вслух."""
    from aiogram import Dispatcher, Router

    dp = Dispatcher()
    router = Router(name="probe")
    dp.include_router(router)
    try:
        dp.include_router(router)
    except RuntimeError:
        return
    raise AssertionError(
        "aiogram больше не запрещает повторный include_router — "
        "перечитайте test_no_router_included_twice, он мог устареть"
    )
