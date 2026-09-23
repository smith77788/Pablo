"""Удалить назначенный прокси нельзя ни из бота, ни из мини-аппа.

`tg_accounts.proxy_id` ссылается на `user_proxies(id)` с ON DELETE SET NULL.
Поэтому удаление прокси, назначенного аккаунтам, ничего не ломает явно — оно
молча обнуляет им proxy_id. Аккаунты после этого подключаются НАПРЯМУЮ с IP
сервера: адрес не совпадает с тем, на котором была поднята сессия, и Telegram
отвечает AUTH_KEY_DUPLICATED. Одна кнопка — и пачка прогретых аккаунтов
мертва. В CLAUDE.md это названо самым дорогим классом багов продукта.

В мини-аппе проверка была. В боте у той же кнопки её не было: хендлер сразу
выполнял DELETE. Разные двери к одному действию разошлись, и опасной осталась
та, которой пользуются чаще.

Тест держит правило на всех дверях сразу: любое удаление из user_proxies
обязано нести проверку назначения, а обе кнопки — ходить через общую функцию.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _py_files():
    for root, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", "tests")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _sql_literals(src: str) -> list[str]:
    """Строковые константы, f-строки — одной склейкой (внутрь не спускаемся).

    Документация пропускается: комментарий про запрос — не запрос.
    """
    tree = ast.parse(src)
    inner = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = n.body[0] if n.body else None
            if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)):
                inner.add(id(doc.value))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            out.append("".join(v.value for v in n.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str)))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in inner:
            out.append(n.value)
    return out


def test_every_proxy_delete_checks_assignment():
    offenders = []
    for path in _py_files():
        try:
            src = open(path, encoding="utf-8").read()
        except OSError:
            continue
        if "user_proxies" not in src:
            continue
        for sql in _sql_literals(src):
            if not re.search(r"DELETE\s+FROM\s+user_proxies", sql, re.I):
                continue
            if "NOT EXISTS" not in sql.upper():
                offenders.append((os.path.relpath(path, _ROOT), sql.strip()[:120]))
    assert not offenders, (
        "удаление прокси без проверки назначения:\n"
        + "\n".join(f"  {p}: {s}" for p, s in offenders)
        + "\nНазначенный прокси удалять нельзя: аккаунты молча уйдут напрямую "
          "и получат AUTH_KEY_DUPLICATED"
    )


def _func(path: str, name: str):
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path}: {name} не найдена — тест устарел")


@pytest.mark.parametrize(
    "path,name",
    [("bot/handlers/proxy_manager.py", "cb_proxy_delete"),
     ("services/mini_app_api.py", "delete_proxy")],
)
def test_both_doors_go_through_the_shared_guard(path, name):
    fn = _func(path, name)
    names = {n.attr if isinstance(n, ast.Attribute) else n.id
             for n in ast.walk(fn) if isinstance(n, (ast.Name, ast.Attribute))}
    assert "delete_proxy_safely" in names, (
        f"{path}:{name} удаляет прокси в обход общей проверки назначения"
    )


# ── поведение самой двери ───────────────────────────────────────────────────


class _Pool:
    """Пул, где DELETE с проверкой назначения не удаляет ничего (прокси занят)."""

    def __init__(self, deleted: int, assigned: int):
        self._deleted, self._assigned = deleted, assigned
        self.executed: list[str] = []

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return f"DELETE {self._deleted}"

    async def fetchval(self, sql, *a):
        return self._assigned


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_assigned_proxy_is_refused_with_a_reason():
    from services.proxy_hygiene import delete_proxy_safely, delete_refusal_text

    res = _run(delete_proxy_safely(_Pool(deleted=0, assigned=3), 77, 5))

    assert res["ok"] is False
    assert res["reason"] == "assigned"
    assert res["assigned"] == 3
    text = delete_refusal_text(res)
    assert "3" in text and "AUTH_KEY_DUPLICATED" in text


def test_free_proxy_is_deleted_in_one_statement():
    from services.proxy_hygiene import delete_proxy_safely

    pool = _Pool(deleted=1, assigned=0)
    res = _run(delete_proxy_safely(pool, 77, 5))

    assert res["ok"] is True
    deletes = [q for q in pool.executed if q.strip().upper().startswith("DELETE")]
    assert len(deletes) == 1, "проверка и удаление должны быть одним запросом"
    assert "NOT EXISTS" in deletes[0].upper(), (
        "между отдельной проверкой и удалением прокси успеют назначить аккаунту"
    )


def test_missing_proxy_is_not_reported_as_deleted():
    from services.proxy_hygiene import delete_proxy_safely, delete_refusal_text

    res = _run(delete_proxy_safely(_Pool(deleted=0, assigned=0), 77, 5))

    assert res["ok"] is False and res["reason"] == "not_found"
    assert "не найден" in delete_refusal_text(res).lower()
