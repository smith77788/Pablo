"""Текст исключения не уходит в сообщение бота мимо `owner_error`.

Владелец по-английски не читает, а `str(исключения)` Telethon/asyncpg — это
английская фраза, иногда с `<…>`, из-за которой Telegram отвергает
HTML-сообщение целиком. У мини-аппа одна дверь для текста ошибки
(`mini_app_api._err`), у бота её не было: обход 08.11.2026 нашёл 46 мест вида
`answer(f"❌ Ошибка: {e}")`. Теперь дверь есть (`bot/utils/owner_error.py`), и
храповик держит, чтобы новое место не прошло мимо неё.
"""
from __future__ import annotations

import pathlib
import re

from bot.utils.owner_error import owner_error

HANDLERS = pathlib.Path(__file__).resolve().parents[1] / "bot" / "handlers"
_SEND = r"(?:answer|edit_text|reply|send_message|_reply)\("
_RAW = re.compile(_SEND + r"[^\n]*?(?:\{(?:e|exc|err|ex)(?:!s)?\}|str\((?:e|exc|err|ex)\))")


def test_no_handler_sends_raw_exception_text():
    hits = []
    for f in sorted(HANDLERS.glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _RAW.search(line):
                hits.append(f"{f.name}:{n}: {line.strip()[:120]}")
    assert not hits, "текст исключения мимо owner_error:\n" + "\n".join(hits)


def test_english_exception_becomes_russian_and_escaped():
    out = owner_error(RuntimeError("Chat admin privileges are required to do that <b>"))
    assert "Chat admin" not in out and "журнал" in out
    assert owner_error(ValueError("Неверный формат <x>")) == "Неверный формат &lt;x&gt;"
    assert owner_error(ValueError("Неверный <x>"), html_escape=False) == "Неверный <x>"
    assert owner_error(None)


def test_alert_text_fits_telegram_popup_limit():
    assert len(owner_error(ValueError("Ошибка " * 100), html_escape=False)) <= 150


# ── То же по дереву разбора, а не по строке ─────────────────────────────────
# Построчная проверка видела только вызов, уместившийся в одну строку. Ответ,
# разнесённый на несколько строк (`await message.answer(\n f"…{escape(str(exc))}"`),
# проходил мимо: так набралось сто мест в 27 обработчиках. Здесь берётся
# имя из `except … as N` и ищется `str(N)` / `{N}` в аргументах отправки.
import ast  # noqa: E402

_SEND_ATTRS = {"answer", "edit_text", "reply", "send_message", "_reply", "edit_caption"}


def _uses_exc(node: ast.AST, name: str) -> bool:
    for x in ast.walk(node):
        if (isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "str"
                and x.args and isinstance(x.args[0], ast.Name) and x.args[0].id == name):
            return True
        if isinstance(x, ast.FormattedValue) and isinstance(x.value, ast.Name) and x.value.id == name:
            return True
    return False


def _ast_hits(root: pathlib.Path) -> list[str]:
    out = []
    for f in sorted(root.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for h in ast.walk(tree):
            if not isinstance(h, ast.ExceptHandler) or not h.name:
                continue
            for st in h.body:
                for c in ast.walk(st):
                    if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                            and c.func.attr in _SEND_ATTRS):
                        continue
                    args = list(c.args) + [k.value for k in c.keywords]
                    if any(_uses_exc(a, h.name) for a in args):
                        out.append(f"{f.name}:{c.lineno}")
    return out


def test_multiline_replies_do_not_send_raw_exception_text():
    hits = _ast_hits(HANDLERS)
    assert not hits, "текст исключения мимо owner_error:\n" + "\n".join(hits)


def test_ast_detector_sees_planted_multiline_reply(tmp_path):
    (tmp_path / "planted.py").write_text(
        "async def h(message):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as _e:\n"
        "        await message.answer(\n"
        "            f'❌ Ошибка: {escape(str(_e)[:200])}',\n"
        "        )\n"
        "    except ValueError as ve:\n"
        "        await message.answer(owner_error(ve))\n",
        encoding="utf-8",
    )
    assert _ast_hits(tmp_path) == ["planted.py:5"]


def test_exception_class_name_is_not_shown_to_owner():
    """«Не удалось собрать план: KeyError» — имя класса тоже английское слово,
    и оно ничего не говорит владельцу. Подробности — в журнале."""
    hits = []
    for f in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if (isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Attribute)
                        and part.value.attr == "__name__"
                        and isinstance(part.value.value, ast.Call)
                        and ast.unparse(part.value.value.func) == "type"):
                    hits.append(f"{f.name}:{node.lineno}")
    assert not hits, "имя класса исключения в тексте:\n" + "\n".join(hits)
