"""Сообщение об ошибке в боте говорит, что случилось и что делать дальше.

Всплывашка «Ошибка.» или «Не найдено.» ничего не объясняет: владелец не
знает, нажать ли кнопку ещё раз, ждать или идти в другой раздел, и через
минуту жмёт ту же кнопку снова. 23.11.2026 таких ответов в меню бота было
35 («Ошибка.», «Ошибка базы данных.», «Ошибка БД.», «Не найден», «Ошибка
экспорта»…). Теперь каждый говорит, что не удалось, и даёт следующий шаг:
«Попробуйте ещё раз через минуту», «Кнопка устарела. Откройте раздел
заново», «Уже удалено или недоступно. Откройте список заново».

Тест читает первый строковый довод ответов (`.answer`, `.edit_text`,
`.reply`, `safe_answer`) во всех обработчиках бота и не пускает голые
формулировки: «Ошибка …» короче пяти слов без подсказки и «Не найден(о/а)»
без продолжения.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "bot" / "handlers"

_CALLS = {"answer", "edit_text", "reply", "safe_answer", "answer_text"}
_BARE = re.compile(
    r"^(ошибка(\s+\S+){0,3}|не\s+найден[оа]?|произошла\s+ошибка|что-то\s+пошло\s+не\s+так)$"
)


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s-]", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


_NEXT_STEP = re.compile(
    r"попробуйте|повторите|начните|откройте|проверьте|заново|позже|добавьте|выберите"
)


def is_bare(text: str) -> bool:
    n = _norm(text)
    return bool(_BARE.match(n)) and not _NEXT_STEP.search(n)


def _first_text(call: ast.Call) -> str | None:
    args = list(call.args)
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    if name == "safe_answer" and args:
        args = args[1:]
    node = args[0] if args else None
    if node is None:
        for kw in call.keywords:
            if kw.arg == "text":
                node = kw.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # f-строка с подстановкой несёт причину («Ошибка: {owner_error(e)}») —
    # это уже не голое сообщение.
    return None


def bare_errors(source: str) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name not in _CALLS:
            continue
        text = _first_text(node)
        if text and is_bare(text):
            out.append((node.lineno, text))
    return out


def test_bare_error_detector_recognises_known_forms() -> None:
    # Пробник: всё, что было в меню до правки, ловится.
    for t in ("Ошибка.", "Ошибка базы данных.", "Ошибка БД.", "Не найден",
              "Не найдено.", "❌ Ошибка при удалении", "Ошибка генерации JSON",
              "Произошла ошибка"):
        assert is_bare(t), t
    # И не ловит то, что объясняет и ведёт дальше.
    for t in ("⚠️ Не удалось загрузить бота. Попробуйте ещё раз через минуту.",
              "Канал не найден. Уже удалено или недоступно. Откройте список заново.",
              "⚠️ Кнопка устарела. Откройте раздел заново.",
              "Ошибка: аккаунт не авторизован — добавьте его заново"):
        assert not is_bare(t), t
    src = 'async def h(callback):\n    await callback.answer("Ошибка.", show_alert=True)\n'
    assert bare_errors(src) == [(2, "Ошибка.")]
    src = 'async def h(callback):\n    await safe_answer(callback, "Не найдено")\n'
    assert bare_errors(src) == [(2, "Не найдено")]
    src = 'async def h(callback, e):\n    await callback.answer(f"Ошибка: {e}")\n'
    assert bare_errors(src) == []
    src = 'async def h(callback):\n    await callback.answer("Ошибка БД. Попробуйте позже.")\n'
    assert bare_errors(src) == []
    src = 'async def h(callback):\n    await safe_answer(callback, "Не найдено")\n'
    assert bare_errors(src) == [(2, "Не найдено")]


def test_bot_errors_tell_what_failed_and_what_next() -> None:
    found = []
    for path in sorted(HANDLERS.rglob("*.py")):
        for line, text in bare_errors(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(ROOT)}:{line}: {text!r}")
    assert not found, (
        "Голое сообщение об ошибке — скажите, что не удалось, и что делать "
        "дальше («Попробуйте ещё раз через минуту», «Откройте список заново»):\n"
        + "\n".join(found)
    )
