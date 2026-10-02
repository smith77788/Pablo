"""Каждая запись терминального статуса операции объявляется наружу.

ПОЧЕМУ ХРАПОВИК. Блок из трёх каналов — график исходов
(`infragram_operations_total`), событие шины `op_done` и подписанная запись
`compliance_engine.record` — был скопирован на каждый путь завершения отдельно.
Каждый НОВЫЙ путь терял его целиком, и это чинилось по одному исходу за раз:

  * отмена закрывалась двумя UPDATE и `return` — ни графика, ни аудита;
  * падение исполнителя — то же самое;
  * голодание по флоту и исчерпанный бюджет живучести закрывались вообще одним
    UPDATE, даже без `result`;
  * сбой в прологе (разбор params, предохранитель, семафор) — тоже.

Причина у всех одна: помнить три канала при добавлении шестого пути завершения
было нечем. Теперь каналы перечислены в `op_worker._announce_op_outcome`, а этот
тест держит связь: функция, которая пишет в очередь терминальный статус, обязана
рядом же объявить исход — сама или делегировав тому, кто это делает.

Тест намеренно неудобный: он падает на каждом новом пути завершения, пока автор
не решит, как этот исход виден снаружи.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = "services/op_worker.py"

# Делегирование считается объявлением: эти помощники сами зовут общую дверь.
_DELEGATES = ("_announce_op_outcome(", "_finish_poisoned_op(",
              "_finish_cancelled_op(", "_finish_fleet_starved_op(")

# Статусы, после которых операцию больше не повторяют (services/op_status.TERMINAL).
_TERMINAL = ("failed", "partial", "cancelled", "done")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _functions(src: str) -> list[tuple[str, str]]:
    """Все функции верхнего уровня: (имя, текст)."""
    out = []
    marks = [(m.start(), m.group(1))
             for m in re.finditer(r"\n(?:async )?def (\w+)\(", src)]
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        out.append((name, src[pos:end]))
    return out


def _writes_terminal_status(body: str) -> bool:
    """Пишет ли функция в operation_queue терминальный статус.

    Смотрим только на часть SET: запросы, двигающие прогресс, к правилу
    отношения не имеют. Литерал статуса ('failed') и параметр (status=$3)
    считаем одинаково — именно переход на параметр позволил бы обойти правило.
    """
    for m in re.finditer(r"UPDATE operation_queue", body):
        seg = body[m.start():m.start() + 900]
        nxt = seg.find("UPDATE operation_queue", 1)
        if nxt > 0:
            seg = seg[:nxt]
        where_at = seg.upper().find("WHERE")
        set_part = seg[:where_at] if where_at > 0 else seg
        if not re.search(r"\bstatus\s*=", set_part):
            continue
        if re.search(r"status\s*=\s*'(%s)'" % "|".join(_TERMINAL), set_part):
            return True
        if re.search(r"status\s*=\s*\$\d", set_part):
            return True
    return False


def test_every_terminal_close_announces_its_outcome():
    src = _read(SRC)
    silent = []
    for name, body in _functions(src):
        if not _writes_terminal_status(body):
            continue
        # Своё собственное имя делегированием не считается: иначе
        # `_finish_cancelled_op` проходил бы правило просто потому, что так
        # называется, и выпал бы из проверки целиком.
        delegates = [d for d in _DELEGATES if d != name + "("]
        if not any(d in body for d in delegates):
            silent.append(name)
    assert not silent, (
        "эти функции закрывают операцию, но наружу об исходе не говорят — "
        "её не будет ни на графике исходов, ни в памяти организма, ни в "
        "подписанном аудит-трейле:\n  " + "\n  ".join(silent)
    )


def test_the_detector_actually_finds_the_closers():
    """Детектор, который ничего не находит, зелёный всегда — правило тогда мертво."""
    src = _read(SRC)
    closers = [name for name, body in _functions(src)
               if _writes_terminal_status(body)]
    assert len(closers) >= 4, (
        f"найдено всего {len(closers)} путей завершения — детектор сломан "
        f"(их заведомо больше: отмена, падение, флот, бюджет живучести, "
        f"сторожа): {closers}"
    )
    for expected in ("_finish_cancelled_op", "_run_op_task"):
        assert expected in closers, (
            f"{expected} закрывает операцию, но детектор его не видит"
        )


def test_the_single_door_lists_all_three_channels():
    src = _read(SRC)
    door = src[src.index("async def _announce_op_outcome("):]
    door = door[:door.index("\nasync def ", 10)]
    assert "infragram_operations_total" in door, "нет графика исходов"
    assert '"op_done"' in door, "память организма не узнаёт о завершении"
    assert "compliance_engine" in door, "нет подписи в аудит-трейле"
    # Ни один канал не имеет права помешать закрытию операции.
    assert door.count("except Exception") >= 3, (
        "каналы объявления не изолированы друг от друга: сбой одного уносит "
        "остальные"
    )


def test_the_door_survives_a_broken_channel():
    """Сбой канала объявления не должен ронять путь завершения."""
    import asyncio

    from services import op_worker

    class _Boom:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("БД недоступна")

    async def _go():
        from services.organism import spine
        _orig = spine.emit

        async def _bad(*a, **k):
            raise RuntimeError("шина недоступна")

        spine.emit = _bad
        try:
            await op_worker._announce_op_outcome(
                _Boom(), 1, 2, "mass_publish", {}, "partial", 3, 1, "итог")
        finally:
            spine.emit = _orig

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


def _run_op_task_source() -> str:
    for name, body in _functions(_read("services/op_worker.py")):
        if name == "_run_op_task":
            return body
    raise AssertionError("в op_worker.py нет функции _run_op_task")


def test_a_close_that_changed_nothing_does_not_report_a_second_outcome():
    """Успешный финал поверх уже закрытой строки не докладывается.

    Строку могли закрыть, пока исполнитель работал: сторож зависших операций,
    dead letter из recovery_engine, отмена владельцем в зазоре перед записью.
    Статус в таком случае не переписывается — условие `status NOT IN
    (терминальные)` даёт ноль строк. Но дальше шло всё остальное завершение по
    ВЫЧИСЛЕННОМУ статусу: владельцу «✅ выполнена» по частичной операции,
    продление расписания поверх закрытия и ВТОРОЙ исход той же операции в
    метриках и подписанном аудите.

    Путь падения эту развилку разбирает давно — здесь храповик на то, чтобы
    успешный путь её не потерял. Проверяется порядок: результат записи
    сохранён, проверен на ноль строк и путь прерван ДО доклада об исходе.
    """
    body = _run_op_task_source()
    assert "_close_res = await _safe_execute(" in body, (
        "результат закрывающей записи снова не сохраняется: ноль строк "
        "(строку закрыл кто-то другой) опять неотличим от успешной записи")
    flat = body.replace(" ", "")
    guard = flat.find('ifstr(_close_res or"").strip().endswith("0")'.replace(" ", ""))
    assert guard > 0, "нет проверки «запись не изменила ни одной строки»"
    close = flat.find("_close_res=await_safe_execute(")
    report = flat.find("await_announce_op_outcome(", close)
    assert close < guard < report, (
        "проверка нулевой записи стоит не между закрытием и докладом об "
        f"исходе: {close} / {guard} / {report}")
    assert "return" in flat[guard:report], (
        "после нулевой записи путь не прерывается — исход доложат вторым")
