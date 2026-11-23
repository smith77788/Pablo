"""Экран, которым кончается пошаговый ввод, не остаётся без кнопок.

Когда обработчик делает `state.clear()`, разговор окончен: бот больше не ждёт
ни текста, ни файла. Если следом он пишет «❌ Файл слишком большой» или
«📤 Публикация поставлена в очередь… Прогресс: /ops» без единой кнопки,
человек упирается в текст. Повторить импорт можно, только вернувшись через
меню вручную; посмотреть операцию — только помня команду.

23.11.2026 таких мест было 20: все ошибки импорта сессий (tdata, .session,
строка, JSON), «слишком много запросов» при входе, три экрана «поставлено в
очередь» в каналах, «диалог стал слишком длинным» у ИИ-помощника, «спасибо»
после отчёта об ошибке. Теперь у ошибок импорта есть «🔄 Попробовать снова»
к тому же способу и «◀️ Назад» к выбору способа, а у очереди — «📋 Детали
операции» (`queued_op_kb`).

Сторож смотрит блоки кода: после `state.clear()` в том же блоке любой ответ
бота (`message.answer`, `.reply`, `.edit_text`) обязан нести `reply_markup`.
Исключения два, и оба честные:
  * строка прогресса («⏳ Публикую…», кончается многоточием) — следом её
    перепишет итоговый экран;
  * следующей же строкой бот присылает экран с кнопками.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "bot" / "handlers"


def _call(stmt: ast.stmt) -> ast.Call | None:
    v = stmt.value if isinstance(stmt, ast.Expr) else None
    if isinstance(v, ast.Await):
        v = v.value
    return v if isinstance(v, ast.Call) else None


def _is_clear(stmt: ast.stmt) -> bool:
    c = _call(stmt)
    return (c is not None and isinstance(c.func, ast.Attribute)
            and c.func.attr == "clear" and "state" in ast.unparse(c.func.value))


def _reply(stmt: ast.stmt) -> ast.Call | None:
    c = _call(stmt)
    if c is None or not isinstance(c.func, ast.Attribute) or not c.args:
        return None
    attr, recv = c.func.attr, ast.unparse(c.func.value)
    if attr in ("edit_text", "edit_caption"):
        return c
    if attr in ("answer", "reply") and "callback" not in recv and recv not in (
        "cb", "call", "query", "c",
    ):
        return c
    return None


def _has_markup(c: ast.Call) -> bool:
    return any(k.arg in ("reply_markup", None) for k in c.keywords)


def _is_progress(c: ast.Call) -> bool:
    node = c.args[0]
    parts: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        parts = [node.value]
    elif isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    text = "".join(parts).strip()
    for tag in ("</b>", "</i>"):
        text = text.replace(tag, "")
    text = text.strip().split("\n")[0].strip()
    return text.endswith("…") or text.endswith("...")


def bare_after_clear(source: str) -> list[int]:
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list) or not block or not isinstance(block[0], ast.stmt):
                continue
            cleared = False
            for i, stmt in enumerate(block):
                if _is_clear(stmt):
                    cleared = True
                    continue
                if not cleared:
                    continue
                c = _reply(stmt)
                if c is None or _has_markup(c) or _is_progress(c):
                    continue
                nxt = _reply(block[i + 1]) if i + 1 < len(block) else None
                if nxt is not None and _has_markup(nxt):
                    continue
                found.append(c.lineno)
    return found


def test_detector_sees_known_dead_end() -> None:
    src = (
        "async def h(message, state):\n"
        "    await state.clear()\n"
        "    await message.answer('❌ Файл слишком большой.')\n"
    )
    assert bare_after_clear(src) == [3]
    ok = (
        "async def h(message, state):\n"
        "    await state.clear()\n"
        "    await message.answer('❌ Файл слишком большой.', reply_markup=kb)\n"
        "    await state.clear()\n"
        "    await message.answer('⏳ Публикую историю…')\n"
        "    await state.clear()\n"
        "    await message.answer('Готово')\n"
        "    await message.answer(text, reply_markup=menu)\n"
    )
    assert bare_after_clear(ok) == []


def test_screen_after_dialog_ends_has_buttons() -> None:
    found = []
    for path in sorted(HANDLERS.rglob("*.py")):
        for line in bare_after_clear(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(ROOT)}:{line}")
    assert not found, (
        "Ввод закончен (state.clear()), а экран без кнопок — добавьте "
        "reply_markup (retry_kb / terminal_kb / queued_op_kb):\n" + "\n".join(found)
    )


def test_queued_op_kb_opens_the_operation() -> None:
    src = (ROOT / "bot" / "utils" / "op_helpers.py").read_text(encoding="utf-8")
    body = src[src.index("def queued_op_kb"):]
    body = body[: body.index("\ndef ", 1)]
    assert 'BmCb(action="op_detail", op_id=op_id)' in body
    assert 'BmCb(action="main")' in body
