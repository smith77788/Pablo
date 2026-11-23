"""Ответ «неверный ввод, попробуйте ещё раз» несёт кнопку «✖️ Отменить».

Шаг мастера, получив неподходящий текст («Название от 1 до 64 символов»,
«Введите число», «Файл слишком большой»), отвечает и остаётся ждать. Если
человек передумал, ему некуда нажать: любое следующее сообщение снова уходит
в тот же шаг и снова получает отказ, а выход — команда /cancel, которую надо
знать. 23.11.2026 таких ответов было 171 в 48 файлах бота. Теперь у каждого
`cancel_kb()`: «✖️ Отменить» ведёт в главное меню, а оно завершает мастер.

Сторож: в обработчике сообщения, повешенном на шаг ввода, ответ
`message.answer(...)`, за которым сразу идёт `return` (бот остаётся на шаге),
обязан нести `reply_markup`. Если шаг в этом же блоке уже сброшен
(`state.clear()`), это не повтор ввода — такие экраны стережёт
test_no_bare_screen_after_dialog_ends.py.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "bot" / "handlers"


def _is_input_step(fn: ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        d = ast.unparse(dec)
        if not d.startswith("router.message(") or "Command" in d:
            continue
        args = d[len("router.message("):]
        if "." in args:
            return True
    return False


def wrong_input_without_cancel(source: str) -> list[int]:
    found: list[int] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.AsyncFunctionDef) or not _is_input_step(fn):
            continue
        for node in ast.walk(fn):
            for field in ("body", "orelse"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if not isinstance(block[i + 1], ast.Return):
                        continue
                    v = stmt.value if isinstance(stmt, ast.Expr) else None
                    if isinstance(v, ast.Await):
                        v = v.value
                    if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                            and v.func.attr in ("answer", "reply") and v.args
                            and "message" in ast.unparse(v.func.value)):
                        continue
                    if any(k.arg in ("reply_markup", None) for k in v.keywords):
                        continue
                    if any(isinstance(x, ast.Expr) and "clear()" in ast.unparse(x)
                           for x in block[:i]):
                        continue
                    found.append(v.lineno)
    return found


def test_detector_sees_known_retry_prompt() -> None:
    src = (
        "@router.message(Wizard.name)\n"
        "async def h(message, state):\n"
        "    if not message.text:\n"
        "        await message.answer('⚠️ Название от 1 до 64 символов:')\n"
        "        return\n"
    )
    assert wrong_input_without_cancel(src) == [4]
    ok = src.replace("символов:')", "символов:', reply_markup=cancel_kb())")
    assert wrong_input_without_cancel(ok) == []
    cleared = (
        "@router.message(Wizard.name)\n"
        "async def h(message, state):\n"
        "    await state.clear()\n"
        "    await message.answer('x')\n"
        "    return\n"
    )
    assert wrong_input_without_cancel(cleared) == []


def test_every_wrong_input_offers_cancel() -> None:
    found = []
    for path in sorted(HANDLERS.rglob("*.py")):
        for line in wrong_input_without_cancel(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(ROOT)}:{line}")
    assert not found, (
        "Шаг ввода отвечает «попробуйте ещё раз» без выхода — добавьте "
        "reply_markup=cancel_kb():\n" + "\n".join(found)
    )
