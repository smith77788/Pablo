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

import ast
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

    Разбор исходов записи живёт в общей двери `_op_closed_by_us` (она же
    делает повтор: блип БД на финале оставлял строку 'running' при уже
    разосланном «✅ выполнена» — см.
    tests/test_the_unsaved_outcome_is_not_reported_postgres.py). Здесь
    проверяется ПОРЯДОК на успешном пути: закрыли, спросили дверь, и только
    потом доклад.
    """
    body = _run_op_task_source()
    assert "_closed = await _op_closed_by_us(" in body, (
        "финал пишется мимо общей двери: ноль строк (строку закрыл кто-то "
        "другой) опять неотличим от успешной записи, а блип БД — от "
        "сохранённого исхода")
    flat = body.replace(" ", "")
    guard = flat.find("ifnot_closed:")
    assert guard > 0, "нет проверки «дверь не подтвердила закрытие»"
    close = flat.find("_closed=await_op_closed_by_us(")
    report = flat.find("await_announce_op_outcome(", close)
    assert close < guard < report, (
        "проверка закрытия стоит не между записью финала и докладом об "
        f"исходе: {close} / {guard} / {report}")
    assert "return" in flat[guard:report], (
        "после неподтверждённого закрытия путь не прерывается — исход доложат "
        "вторым")


def test_every_terminal_write_goes_through_the_one_door():
    """Храповик: терминальный статус операции пишется ТОЛЬКО общей дверью.

    Путей завершения пять (успех, падение, сбой пролога, отмена, не дождались
    флота), и у записи финала три исхода. Пока каждый путь разбирал их сам,
    четыре из пяти знали ровно один исход — «ноль строк», — а на
    незаписанной записи шли докладывать владельцу об успехе, которого в БД
    нет. Новый путь завершения, написанный по образцу соседнего, повторил бы
    это снова, поэтому дверь одна: `_op_closed_by_us`.

    Терминальной считаем запись, которая ставит `finished_at` в
    `operation_queue`: возвраты в очередь и отложенные повторы его не трогают.
    """
    src = _read("services/op_worker.py")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_safe_execute", "_retrying_execute")):
            continue
        if len(node.args) < 2:
            continue
        sql = ast.get_source_segment(src, node.args[1]) or ""
        flat = re.sub(r"\s+", " ", sql)
        if "operation_queue" in flat and "finished_at" in flat:
            offenders.append((node.lineno, flat[:80]))
    assert not offenders, (
        "терминальный статус операции пишется мимо `_op_closed_by_us` — этот "
        "путь не отличит «закрыл кто-то другой» и «запись не прошла» от "
        f"успешной записи: {offenders}")


def test_the_one_door_detector_bites():
    """Самопроверка храповика: на заведомо больном тексте он обязан найти."""
    sick = ('await _safe_execute(pool, "UPDATE operation_queue SET status=$1, '
            'finished_at=now() WHERE id=$2", s, i)')
    tree = ast.parse(sick)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_safe_execute", "_retrying_execute")
                and len(node.args) >= 2):
            sql = ast.get_source_segment(sick, node.args[1]) or ""
            flat = re.sub(r"\s+", " ", sql)
            if "operation_queue" in flat and "finished_at" in flat:
                found.append(node.lineno)
    assert found, "детектор не видит запись финала мимо двери — он сломан"


# ── Та же проверка видит и запись, сделанную ЧЕРЕЗ fetch ────────────────────
#
# Храповик выше смотрит только на `_safe_execute`/`_retrying_execute` и только
# в `services/op_worker.py`. Запись с `RETURNING` выдаётся не через execute, а
# через fetch/fetchrow — и такой терминальный UPDATE для него невидим ПО
# ПОСТРОЕНИЮ. Сейчас таких три, и все три — осознанный приём «погасить одним
# атомарным UPDATE, а итог и доклад дописать вторым шагом» (так и сказано в
# докстринге `_finish_poisoned_op`). Но ничто этого не держало: новый путь мог
# бы закрыть операцию через `fetchrow` и не объявить исход вовсе, оставшись
# зелёным.

_TREE_SKIP_DIRS = {"tests", "__pycache__", ".git", "node_modules", "venv",
                   ".venv", "migrations"}

# Атомарное гашение с RETURNING, у которого итог дописывает ВТОРОЙ шаг.
# Исключение проверяемое: тест ниже требует, чтобы путь передавал строку
# дозакрывающей функции, а не просто значился в этом словаре.
CLOSES_THEN_HANDS_OFF = {
    "_reset_stale_running": "_finish_poisoned_op",
    "_watchdog_stale": "_finish_poisoned_op",
    "_dead_letter_op": "_announce_op_outcome",
}

_ANNOUNCING_CLOSERS = ("_op_closed_by_us", "_finish_poisoned_op",
                       "_finish_cancelled_op", "_announce_op_outcome")


def _terminal_writes_anywhere():
    """Терминальные записи (`finished_at` в SET) любым доступом к БД."""
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            if "operation_queue" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                door = ast.unparse(node.func).split(".")[-1]
                sql = " ".join(ast.unparse(a) for a in node.args[:3])
                if not re.search(r"UPDATE\s+operation_queue", sql):
                    continue
                set_part = re.split(r"\bWHERE\b", sql, flags=re.I)[0]
                if "finished_at" not in set_part:
                    continue
                fn, span = "?", None
                for g in ast.walk(tree):
                    if (isinstance(g, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and g.lineno <= node.lineno <= (g.end_lineno or g.lineno)):
                        s = (g.end_lineno or g.lineno) - g.lineno
                        if span is None or s < span:
                            fn, span = g.name, s
                out.append((rel, fn, door, node.lineno))
    return out


def test_the_terminal_census_sees_the_fetch_shaped_writes():
    """Самопроверка: запись с RETURNING обязана попадать в выборку."""
    found = {fn for _, fn, _, _ in _terminal_writes_anywhere()}
    for name in CLOSES_THEN_HANDS_OFF:
        assert name in found, (
            f"`{name}` закрывает операцию через RETURNING, но перепись его не "
            "видит — отбор снова смотрит только на execute")
    assert "_run_op_task" in found, "перепись не видит обычную запись финала"


def test_every_terminal_write_announces_or_hands_off():
    bad = [(rel, fn, door, ln)
           for rel, fn, door, ln in _terminal_writes_anywhere()
           if door not in _ANNOUNCING_CLOSERS and fn not in CLOSES_THEN_HANDS_OFF]
    assert not bad, (
        "операция закрывается терминально мимо объявляющей двери: "
        f"{bad}. Такой путь не отличит «закрыл кто-то другой» от «запись не "
        "прошла», и исход не попадёт ни на график, ни в аудит-трейл. Если "
        "итог дописывает второй шаг — впишите путь в CLOSES_THEN_HANDS_OFF, "
        "указав, КОМУ он передаёт строку")


def test_the_hand_off_excuse_is_checkable():
    """Исключение обязано быть видно в коде: путь реально передаёт строку.

    Иначе словарь превращается в записку «мы подумали, что тут всё хорошо», а
    именно такие записки и переживают правку, которая их опровергает.
    """
    for fn_name, closer in CLOSES_THEN_HANDS_OFF.items():
        found_in = None
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(base, f)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                if f"def {fn_name}" not in src:
                    continue
                node = next((n for n in ast.walk(ast.parse(src))
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.name == fn_name), None)
                if node is None:
                    continue
                body = "\n".join(
                    src.splitlines()[node.lineno - 1:node.end_lineno])
                found_in = (os.path.relpath(path, ROOT), body)
                break
            if found_in:
                break
        assert found_in, f"`{fn_name}` из CLOSES_THEN_HANDS_OFF больше нет"
        rel, body = found_in
        assert closer in body, (
            f"{rel}: `{fn_name}` гасит операцию, но строку `{closer}` больше "
            "не передаёт — исход останется необъявленным, а храповик молчит")
