"""Сообщение об ошибке, которое увидит владелец, обязано быть на русском.

Владелец Infragram не читает по-английски — это записано в CLAUDE.md первой
строкой. Английская строка в `_err(...)` доходит до него буквально: мини-апп
показывает её текстом в тосте, и вместо «что делать» он видит
«Invalid Telegram initData».

Так в API-слое и жили четыре штуки: подпись Telegram не подтвердилась («Invalid
Telegram initData»), роль бота («role must be one of ('entry', ...)» — кортеж
Python как подсказка пользователю) и дважды префикс «Telegram API: » перед
описанием от Телеграма.

Проверка читает ВЕСЬ API-слой разбором, а не текстом: аргумент `_err` берётся
из дерева, поэтому ни перенос ручки в другой модуль, ни f-строка её не слепят.
Технические маркеры статуса (`Unauthorized`, `Forbidden`) оставлены: их
мини-апп не печатает, а разбирает — на 401 он ведёт на повторный вход.
"""
from __future__ import annotations

import ast
import re

from tests.miniapp_source import api_files

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# Маркеры протокола, которые мини-апп разбирает, а не показывает.
PROTOCOL_MARKERS = {"Unauthorized", "Forbidden", "Not found", "OK", "ok"}


def _err_messages() -> list[tuple[str, int, str]]:
    """(файл, строка, литерал) для каждого вызова `_err` с текстом в коде."""
    out = []
    for path in api_files():
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_err" and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append((path.name, arg.lineno, arg.value))
            elif isinstance(arg, ast.JoinedStr):
                # У f-строки проверяем только её СОБСТВЕННЫЙ текст: подставленное
                # значение (описание от Телеграма, имя канала) переводить нечем.
                literal = "".join(v.value for v in arg.values
                                  if isinstance(v, ast.Constant) and isinstance(v.value, str))
                out.append((path.name, arg.lineno, literal))
    return out


def test_the_detector_finds_the_error_messages():
    """Самопроверка измерителя: пустой список — это сломанный измеритель."""
    found = _err_messages()
    assert len(found) > 300, f"сообщения об ошибке потерялись: {len(found)}"
    assert any(CYRILLIC.search(t) for _f, _l, t in found), (
        "ни одного русского сообщения — проверка читает не тот код")


def test_no_english_error_reaches_the_owner():
    offenders = [
        f"{f}:{ln}: {text[:70]}"
        for f, ln, text in _err_messages()
        if (stripped := text.strip())
        and len(stripped) > 2
        and stripped not in PROTOCOL_MARKERS
        and not CYRILLIC.search(stripped)
    ]
    assert not offenders, (
        "владелец не читает по-английски, а эти сообщения он увидит дословно:\n  "
        + "\n  ".join(offenders))
