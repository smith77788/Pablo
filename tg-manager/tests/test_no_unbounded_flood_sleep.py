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

# Фоновые циклы вне op_worker, где действует ТО ЖЕ правило. Они спят не на
# FloodWait от MTProto, а на `retry_after` от Bot API — величина та же по смыслу
# и так же назначается Telegram. Цикл здесь один на всех получателей, поэтому
# цена безграничного сна даже выше: 429 на ОДНОМ адресате останавливал рассылку,
# шаг воронки и пересылку оператору целиком.
_EXTRA_TARGETS = (
    os.path.join(ROOT, "services", "broadcaster.py"),
    os.path.join(ROOT, "services", "funnel_runner.py"),
    os.path.join(ROOT, "services", "relay.py"),
)

# Имена, в которых лежит длительность флуд-паузы.
#
# Имя в этом списке — единственное, по чему детектор узнаёт флуд-сон, поэтому
# новую переменную под паузу надо называть одним из этих имён либо дописывать
# сюда. `flood` и `retry_after` попали позже остальных: до них проходили мимо
# храповика две паузы bulk-публикации (звались `flood`) и сны фоновых циклов на
# паузе от Bot API (звались `retry_after`).
_FLOOD_NAMES = {"flood_wait", "fw", "flood_wait_s", "wait_s", "flood",
                "retry_after"}

# Как сон может быть ограничен. Достаточно любого из способов.
_GUARD_MARKERS = ("_FLOOD_INLINE_MAX_S", "600", "wait_for_retry_after")

# `max(backoff(...), flood)` ограничивает ТОЛЬКО своё второе слагаемое: бэкофф
# у него с потолком, а флуд-пауза проходит целиком. Выглядит как предел, пределом
# не является — поэтому сон, длина которого посчитана через max(), храповик
# считает неограниченным независимо от окружающих строк.
_FAKE_GUARD_CALLS = ("max",)

# Сон через общую обёртку ограничен по построению — её и ищем в первую очередь.
_BOUNDED_CALL = "bounded_flood_sleep"


def _source(path: str | None = None) -> str:
    with open(path or TARGET, encoding="utf-8") as f:
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


def _fake_guarded(call: ast.Call) -> bool:
    """Длина сна посчитана через max() — предел мнимый (см. _FAKE_GUARD_CALLS)."""
    for n in ast.walk(call.args[0]):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in _FAKE_GUARD_CALLS:
                return True
    return False


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
        if _fake_guarded(call):
            unbounded.append((call.lineno, lines[call.lineno - 1].strip()))
            continue
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


def test_background_loops_never_sleep_on_raw_retry_after():
    """Рассылка, воронка и пересылка оператору — тот же класс, что op_worker.

    `await asyncio.sleep(retry_after)` в общем цикле останавливает доставку ВСЕМ
    из-за лимита на ОДНОМ. Ждать должен следующий круг цикла, а не текущий:
    услуга `services.flood_sleep.wait_for_retry_after` ждёт короткую паузу и
    честно отказывается от длинной, возвращая False.
    """
    unbounded = []
    for path in _EXTRA_TARGETS:
        src = _source(path)
        lines = src.splitlines()
        for call in _flood_sleeps(ast.parse(src)):
            context = _guarding_lines(lines, call.lineno)
            if not any(marker in context for marker in _GUARD_MARKERS):
                unbounded.append(
                    f"  {os.path.basename(path)}:{call.lineno}: "
                    f"{lines[call.lineno - 1].strip()}"
                )
    assert not unbounded, (
        "сон на сыром retry_after останавливает весь фоновый цикл:\n"
        + "\n".join(unbounded)
        + "\nИспользуйте services.flood_sleep.wait_for_retry_after()."
    )


def test_bounded_wait_helper_exists_and_is_capped():
    """Правило требует альтернативы — она обязана быть на месте и с пределом."""
    from services import flood_sleep

    assert flood_sleep.MAX_RETRY_AFTER_S > 0
    assert flood_sleep.MAX_RETRY_AFTER_S <= 3600


def test_long_retry_after_is_refused_not_slept_through():
    """Длинную паузу обёртка НЕ спит: возвращает False, не задержав цикл."""
    import asyncio
    import time

    from services import flood_sleep

    started = time.monotonic()
    waited = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        flood_sleep.wait_for_retry_after(
            flood_sleep.MAX_RETRY_AFTER_S + 1, where="test")
    )
    assert waited is False
    assert time.monotonic() - started < 1.0


def test_short_retry_after_is_actually_awaited():
    """Короткую паузу обёртка выдерживает целиком — иначе повтор поймает лимит
    подлиннее. Половинчатого сна быть не должно."""
    import asyncio

    from services import flood_sleep

    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        slept: list[float] = []

        async def _fake_sleep(sec):
            slept.append(sec)

        real = asyncio.sleep
        asyncio.sleep = _fake_sleep          # noqa: F811 — подмена на время теста
        try:
            waited = loop.run_until_complete(
                flood_sleep.wait_for_retry_after(3, where="test", extra_s=5))
        finally:
            asyncio.sleep = real
        assert waited is True
        assert slept == [8.0]                # пауза + запас, ничего не урезано
    finally:
        loop.close()
def test_max_does_not_count_as_a_bound():
    """Сам детектор обязан считать max(backoff, flood) НЕограниченным.

    Две паузы bulk-публикации так и выглядели — и проходили храповик, потому что
    рядом стояло слово-маркер. Без этой проверки правило можно обойти обратно
    одной строкой.
    """
    tree = ast.parse("import asyncio\nasync def f(flood, backoff):\n"
                     "    await asyncio.sleep(max(backoff(1), flood))\n")
    sleeps = _flood_sleeps(tree)
    assert sleeps, "детектор не увидел сон по имени flood"
    assert _fake_guarded(sleeps[0]), "max() принят за предел — храповик дырявый"
