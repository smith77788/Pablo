"""Храповик: ни один исполнитель не спит на флуд-паузе без предела.

Telegram отдаёт FloodWait и на десятки секунд, и на часы. Сон ровно на столько,
сколько он попросил, держит слот параллельности (один из восьми) и арендованные
аккаунты всё это время, не делая ничего: флот простаивает, очередь владельца не
двигается, а с введением потолка прогона такой сон ещё и съедает его целиком,
обрывая всю операцию.

Это правило легко нарушить заново: `await asyncio.sleep(flood_wait)` выглядит
безобидно и «правильно» — мы же уважаем просьбу платформы. Уважать её надо, но
ждать должна ОЧЕРЕДЬ, а не занятый слот: операция откладывается через
`_defer_op_for_flood` (или, где отложить нельзя, действие пропускается), и
пауза выдерживается полностью.

Поэтому проверка не на конкретное место, а на весь файл: каждый сон, длина
которого приходит из флуд-паузы, обязан быть под защитой предела.

op_worker импортирует telethon и в тестовой среде не поднимается — проверяем
исходником, как и соседние тесты очереди.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "services", "op_worker.py")

# Имена, в которых лежит длительность флуд-паузы.
_FLOOD_NAMES = {"flood_wait", "fw", "flood_wait_s", "wait_s"}

# Как сон может быть ограничен. Достаточно любого из способов.
_GUARD_MARKERS = ("_FLOOD_INLINE_MAX_S", "600")

# Сон через общую обёртку ограничен по построению — её и ищем в первую очередь.
_BOUNDED_CALL = "bounded_flood_sleep"


def _source() -> str:
    with open(TARGET, encoding="utf-8") as f:
        return f.read()


def _flood_sleeps(tree: ast.AST) -> list[ast.Call]:
    """Вызовы asyncio.sleep, длительность которых приходит из флуд-паузы."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == _BOUNDED_CALL:
            continue  # общая обёртка ограничена по построению
        if not (isinstance(func, ast.Attribute) and func.attr == "sleep"):
            continue
        if not node.args:
            continue
        names = {
            n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)
        }
        if names & _FLOOD_NAMES:
            found.append(node)
    return found


def _guarding_lines(src_lines: list[str], lineno: int, window: int = 30) -> str:
    """Строки вокруг сна — там, где стоит проверка предела."""
    lo = max(0, lineno - 1 - window)
    return "\n".join(src_lines[lo:lineno])


def test_flood_sleeps_are_detected_at_all():
    """Сам детектор обязан что-то находить, иначе тест зелёный по ошибке.

    Детектор, который ничего не видит, проходит всегда — и правило тихо
    перестаёт действовать.
    """
    sleeps = _flood_sleeps(ast.parse(_source()))
    assert sleeps, "детектор флуд-снов не нашёл ни одного — проверьте _FLOOD_NAMES"


def test_every_flood_sleep_is_bounded():
    src = _source()
    lines = src.splitlines()
    unbounded = []
    for call in _flood_sleeps(ast.parse(src)):
        context = _guarding_lines(lines, call.lineno)
        if not any(marker in context for marker in _GUARD_MARKERS):
            unbounded.append((call.lineno, lines[call.lineno - 1].strip()))
    assert not unbounded, (
        "флуд-сон без предела держит слот и аккаунты часами:\n"
        + "\n".join(f"  строка {ln}: {code}" for ln, code in unbounded)
        + "\nОтложите операцию через _defer_op_for_flood или пропустите действие."
    )


def test_the_defer_mechanism_exists_for_executors_to_use():
    """Правило выше требует альтернативы — она обязана быть на месте."""
    src = _source()
    assert "async def _defer_op_for_flood" in src
    assert "_FLOOD_INLINE_MAX_S" in src


def test_global_presence_username_flood_is_capped():
    """Канал уже создан; ждать часы ради ОДНОГО имени слот не должен."""
    src = _source()
    seg = src[src.index("async def _exec_global_presence_channel"):]
    seg = seg[:seg.index("имя не ставим") + 200]
    assert "if flood_wait > _FLOOD_INLINE_MAX_S:" in seg
    assert "username" in seg
