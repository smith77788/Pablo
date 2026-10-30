"""Защитная запись, съеденная ОТМЕНОЙ задачи, обязана оставить след.

РАЗРЫВ. Три писателя защиты (`_journal_write`, `_journal_writemany`,
`_retrying_execute`) построены одинаково: попытка, пауза, вторая попытка, потом
`log.error` и счётчик — «чтобы риск дубля был виден на графике, а не только в
логе». Ловят они `except Exception`. А `asyncio.CancelledError` с Python 3.8 не
`Exception`, поэтому отмена проходила сквозь всю эту конструкцию: ни лога, ни
счётчика, ни единого следа. Вся система защитных оповещений была слепа ровно к
тому пути, которым пользуются наши собственные остановки — `shutdown` по
SIGTERM и `_stand_down_after_lease_loss` при передаче очереди.

ЧЕМ ЭТО ПЛОХО ИМЕННО ЗДЕСЬ. `_journal_write` сама объясняет, что пишется
ПОСЛЕ доставки: «цель уже отработана, отменять нечего». Отмена, попавшая между
действием и отметкой, теряет отметку о СОСТОЯВШЕМСЯ действии. А `shutdown`
возвращает свои операции в очередь, то есть повторный прогон не «возможен», он
ГАРАНТИРОВАН следующим процессом — и этот прогон сделает цель второй раз:
второе сообщение человеку, второй пост, второе приглашение. Автодеплой означает
рестарт на каждый пуш, так что путь этот не экзотический.

ПОЧЕМУ СЧЁТЧИК ОТДЕЛЬНЫЙ И НЕ БУДИТ ВЛАДЕЛЬЦА. Потеря по отмене — не отказ
базы, и поднимать ею тревогу `_PROTECTIVE_FAILURE_COUNTERS` нельзя: деплой
штатный и частый, а оповещение, приходящее на каждый выпуск, владелец
перестанет читать — и пропустит настоящий сбой защиты. Поэтому отдельный
счётчик `infragram_protective_write_cancelled_total` (не `*_failures_total`,
чтобы храповик защитных счётчиков не требовал его в списке тревог) и WARNING в
логе: потеря перестаёт быть невидимой, но тревогу не обесценивает.

ОТМЕНА ПРОБРАСЫВАЕТСЯ ДАЛЬШЕ. Проглотить её нельзя ни в коем случае: задача,
не заметившая отмены, продолжает работать сессией, аренда которой уже снята, —
это AUTH_KEY_DUPLICATED. Контракт писателей при этом не меняется: ошибок они
по-прежнему не бросают, а отмена проходила сквозь них и раньше.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

CANCELLED = "infragram_protective_write_cancelled_total"


@pytest.fixture
def ow():
    from services import op_worker
    yield op_worker


@pytest.fixture(autouse=True)
def _fresh_metrics():
    from services import metrics as _m
    _m.reset()
    _m.set_shutting_down(False)
    yield
    _m.reset()
    _m.set_shutting_down(False)


def _counter(name: str) -> float:
    from services import metrics as _m
    counters = (_m.snapshot() or {}).get("counters") or {}
    return sum(float(v) for k, v in counters.items()
               if k == name or k.startswith(name + "{"))


class _CancellingPool:
    """Пул, у которого запись попадает под отмену задачи."""

    async def execute(self, q, *a):
        raise asyncio.CancelledError()

    async def executemany(self, q, rows):
        raise asyncio.CancelledError()


class _BrokenPool:
    """Пул, у которого запись отказывает по-настоящему (не отмена)."""

    async def execute(self, q, *a):
        raise RuntimeError("БД недоступна")

    async def executemany(self, q, rows):
        raise RuntimeError("БД недоступна")


def _call(ow, name, pool):
    if name == "_journal_writemany":
        return ow._journal_writemany(
            pool, "INSERT INTO operation_log VALUES ($1)", [(1,)],
            log_ctx="[t]", where="t")
    fn = getattr(ow, name)
    return fn(pool, "UPDATE operation_queue SET status='done'",
              log_ctx="[t]", where="t")


WRITERS = ["_journal_write", "_journal_writemany", "_retrying_execute"]


# ── Главное: отмена оставляет след и летит дальше ────────────────────────────

@pytest.mark.parametrize("writer", WRITERS)
def test_a_cancelled_write_is_counted_and_reraised(ow, writer, caplog):
    """Счётчик, WARNING и проброс отмены — все три обязательны."""
    async def _go():
        with caplog.at_level(logging.WARNING, logger="services.op_worker"):
            with pytest.raises(asyncio.CancelledError):
                await _call(ow, writer, _CancellingPool())

    asyncio.run(_go())

    assert _counter(CANCELLED) >= 1, (
        f"{writer}: запись съела отмена, а следа нет — потеря невидима"
    )
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.levelno >= logging.WARNING)
    assert text.strip(), f"{writer}: ни строчки в логе об отменённой записи"


@pytest.mark.parametrize("writer", WRITERS)
def test_a_real_database_failure_still_goes_the_old_way(ow, writer):
    """Самопроверка мерки: отказ БД — это НЕ отмена, и счётчик отмен молчит.

    Счётчик, срабатывающий на любой потере, не отличал бы деплой от сбоя базы,
    то есть не измерял бы ничего.
    """
    async def _go():
        return await _call(ow, writer, _BrokenPool())

    res = asyncio.run(_go())

    assert res == "ERROR", f"{writer}: отказ БД перестал возвращать ERROR: {res!r}"
    assert _counter(CANCELLED) == 0, (
        f"{writer}: счётчик отмен сработал на обычном отказе БД — мерка сломана"
    )


def test_the_cancelled_counter_is_not_an_owner_alarm(ow):
    """Он НЕ в списке тревог: деплой штатный, а тревога на каждый выпуск — шум."""
    assert CANCELLED not in ow._PROTECTIVE_FAILURE_COUNTERS, (
        "счётчик отмен попал в список тревог владельца — он будет приходить на "
        "каждый деплой, и настоящие сбои защиты потеряются в этом шуме"
    )


def test_the_counter_is_documented(ow):
    """Описание графика обязано быть: безымянный счётчик никто не прочтёт."""
    from services import metrics as _m
    assert CANCELLED in _m._HELP, "счётчик не описан в services/metrics.py::_HELP"


# ── Храповик: слепота не должна вернуться ────────────────────────────────────

import ast  # noqa: E402  (рядом с переписью, чтобы читалось как одна тема)
import os  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TREE = ("services", "bot", "database", "codex_bridge", "main.py")

# Считаем потерю ЗАПИСИ. Счётчики чтения (`*_read_failures_total`) сюда
# намеренно не входят: отменённое чтение ничего не теряет — работа ещё не
# начиналась, и прогон просто откладывается целиком.
_LOSS_SUFFIXES = ("_write_failures_total", "_unmark_failures_total")


def _py_files():
    out = []
    for rel in _TREE:
        path = os.path.join(ROOT, rel)
        if os.path.isfile(path):
            out.append(path)
            continue
        for base, _dirs, names in os.walk(path):
            for n in sorted(names):
                if n.endswith(".py"):
                    out.append(os.path.join(base, n))
    return out


def _loss_counters(handler) -> list[str]:
    return sorted({
        n.value for n in ast.walk(handler)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.startswith("infragram_")
        and n.value.endswith(_LOSS_SUFFIXES)
    })


def _blind_writers(tree, label: str) -> list[str]:
    """try-блоки, которые СЧИТАЮТ потерю записи и не ловят отмену."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught = []
        for h in node.handlers:
            if h.type is None:
                caught.append("<bare>")
                continue
            items = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
            caught += [ast.unparse(i) for i in items]
        if any("CancelledError" in c or c in ("BaseException", "<bare>")
               for c in caught):
            continue
        for h in node.handlers:
            names = _loss_counters(h)
            if names:
                found.append(f"{label}:{h.lineno} {','.join(names)}")
                break
    return found


def test_the_census_sees_a_deliberately_blind_writer():
    """Самопроверка мерки ПРЕЖДЕ, чем верить её пустому списку."""
    sample = (
        "async def w(pool):\n"
        "    for a in (1, 2):\n"
        "        try:\n"
        "            return await pool.execute('x')\n"
        "        except Exception:\n"
        "            inc('infragram_demo_write_failures_total')\n"
    )
    assert _blind_writers(ast.parse(sample), "sample"), (
        "перепись не видит заведомо слепого писателя — верить её пустому "
        "списку нельзя"
    )
    healthy = sample.replace(
        "        except Exception:",
        "        except asyncio.CancelledError:\n            raise\n"
        "        except Exception:")
    assert not _blind_writers(ast.parse(healthy), "sample"), (
        "перепись считает слепым писателя, который отмену ловит — мерка сломана"
    )


def test_no_protective_writer_is_blind_to_cancellation():
    """Потеря ЗАПИСИ, которую мы считаем, обязана считаться и на пути отмены.

    `except Exception` не ловит `CancelledError` с Python 3.8, то есть writer
    без своего `except asyncio.CancelledError` слеп ровно к нашим собственным
    остановкам: SIGTERM на деплое (а деплой у нас на каждый пуш) и передача
    очереди другому процессу. Исключений в этом списке нет намеренно: на
    момент постановки храповика их было ноль, и каждое новое — это настоящая
    невидимая потеря, а не издержка мерки.
    """
    blind = []
    for path in _py_files():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        blind += _blind_writers(tree, os.path.relpath(path, ROOT))
    assert not blind, (
        "писатели защиты считают потерю записи, но слепы к отмене "
        f"({len(blind)}): {blind}"
    )


# ── Плановый выпуск и внеплановая передача очереди — не одно и то же ─────────

UNPLANNED = "infragram_unplanned_write_cancel_failures_total"


def test_an_unplanned_loss_raises_the_alarm(ow):
    """Передача очереди или снятие зависшего прогона — тревога владельцу.

    Процесс при этом ЖИВ и очередь достаётся другому: потеря ровно та же, а
    повод к ней редкий, так что шума тревога не даёт.
    """
    async def _go():
        with pytest.raises(asyncio.CancelledError):
            await _call(ow, "_journal_write", _CancellingPool())

    asyncio.run(_go())

    assert _counter(UNPLANNED) >= 1, (
        "внеплановая потеря защитной записи не поднимает тревогу — владелец "
        "узнает о повторных действиях только из жалоб"
    )


def test_a_planned_shutdown_loses_the_same_write_without_an_alarm():
    """На деплое тревоги быть не может: он на каждый пуш, и её перестанут читать."""
    from services import metrics as _m
    _m.set_shutting_down(True)

    async def _go():
        from services import op_worker as _ow
        with pytest.raises(asyncio.CancelledError):
            await _call(_ow, "_journal_write", _CancellingPool())

    asyncio.run(_go())

    assert _counter(CANCELLED) >= 1, "потеря на деплое перестала считаться вовсе"
    assert _counter(UNPLANNED) == 0, (
        "плановый выпуск поднял тревогу — она будет приходить на каждый пуш"
    )


def test_the_alarm_counter_reaches_the_owner(ow):
    """Внеплановая потеря обязана быть в списке защитных тревог."""
    assert UNPLANNED in ow._PROTECTIVE_FAILURE_COUNTERS, (
        "счётчик внеплановой потери не в _PROTECTIVE_FAILURE_COUNTERS — "
        "владелец о нём не узнает"
    )


def test_shutdown_tells_the_metrics_it_is_planned():
    """Без этой проводки весь раздел не работает: флаг никто не поставит."""
    import inspect
    import textwrap

    from services import op_worker as _ow

    src = inspect.getsource(_ow.shutdown)
    tree = ast.parse(textwrap.dedent(src))
    calls = [ast.unparse(n.func) for n in ast.walk(tree)
             if isinstance(n, ast.Call)]
    assert any("set_shutting_down" in c for c in calls), (
        "shutdown не отмечает плановую остановку в метриках — тогда деплой "
        "неотличим от передачи очереди, и тревога пойдёт на каждый пуш"
    )
