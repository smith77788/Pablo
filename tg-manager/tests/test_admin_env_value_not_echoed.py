"""Правка переменной окружения в админке не печатает секрет в чат.

Ответ «Переменная обновлена» показывал первые 80 символов значения — токен
бота, ключ ИИ, пароль базы целиком, — а сообщение админа со значением
оставалось в истории. Тест падает без фикса.
"""
from __future__ import annotations

import ast
import pathlib

from bot.handlers.admin import mask_env_value

SRC = (pathlib.Path(__file__).resolve().parents[1] / "bot" / "handlers" / "admin.py").read_text(encoding="utf-8")


def test_mask_hides_the_middle():
    tok = "123456789:AAEhBP0av28bGtUcLzZ2Dz5UudzRo7PkLg0"
    m = mask_env_value(tok)
    assert m.startswith("1234") and m.endswith("kLg0") and "AAEhBP0av28" not in m
    assert mask_env_value("short") == "****"
    assert mask_env_value("") == ""


def test_env_branches_delete_message_and_mask():
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_admin_message")
    body = ast.get_source_segment(SRC, fn)
    for marker in ('elif state.startswith("env_edit:"):', 'elif state == "env_add":'):
        i = body.index(marker)
        j = body.find("\n    elif ", i + 10)
        branch = body[i: j if j > 0 else len(body)]
        assert "_delete_secret_message(message)" in branch, marker
        assert "mask_env_value(" in branch, marker
        assert "[:80]" not in branch, marker
