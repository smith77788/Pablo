"""Сдача аренды, которая НЕ остановила работу, обязана сказать об этом вслух.

РАЗРЫВ. `_stand_down_after_lease_loss` держится на одном допущении, и сама же
его объявляет: «Сначала гасим задачи, ПОТОМ отпускаем аренды аккаунтов: пока
задача не свернулась, она ещё работает сессией, и отпустить аренду раньше
значит отдать живую сессию новому держателю». Порядок соблюдён. А вот
выполнение допущения не проверялось: после `asyncio.wait(..., timeout=grace_s)`
число свернувшихся задач считалось в `stopped` — и на этом всё. Аренды
аккаунтов снимались безусловно, `stopped` уходил в возвращаемое значение,
которое вызывающая сторона в `run` отбрасывает, и ни лога уровня ERROR, ни
счётчика у этого случая не было.

ЧТО ЭТО ЗНАЧИТ НА ПРАКТИКЕ. Задача не обязана свернуться за отведённые пять
секунд. Уборка исполнителя идёт через `_retrying_execute` — запись итога,
журнал целей, снятие предзаписи инвайта, — а он повторяет попытки с паузами.
Один сетевой сбой БД в момент сдачи аренды, и уборка одной операции легко
занимает десятки секунд. В эти десятки секунд процесс ЖИВ (в отличие от
`shutdown`, где та же форма кода безопасна ровно потому, что процесс умирает
вместе с сессиями), задача продолжает работать сессией, а её аренда уже снята —
новый держатель вправе забрать аккаунт немедленно. Два процесса на одной сессии
— это AUTH_KEY_DUPLICATED и сгоревший аккаунт, то есть ровно та поломка, от
которой аренда и защищает.

ПОЧЕМУ ЛЕЧИМ ГРОМКОСТЬЮ, А НЕ УДЕРЖАНИЕМ АРЕНДЫ. Не отпускать аренду нельзя:
аренда всё равно истечёт по TTL, и новый держатель заберёт аккаунт законно, а
наша запись о занятости останется висеть. Поэтому второй, тоже ограниченный
шанс свернуться — и, если он не помог, аренды всё равно снимаются, но случай
перестаёт быть невидимым: ERROR в логе и защитный счётчик, который доходит до
владельца через `_watchdog_protective_failures`.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


@pytest.fixture
def ow():
    from services import op_worker
    yield op_worker


@pytest.fixture(autouse=True)
def _clean_state(ow):
    from services import metrics as _m
    _m.reset()
    # Флаг плановой остановки — глобал процесса: его мог оставить включённым
    # ЧУЖОЙ тест (любой, кто звал `shutdown`), и тогда проверка тревоги при
    # передаче очереди зависела бы от порядка запуска.
    _m.set_shutting_down(False)
    ow._shutting_down = False
    yield
    ow._active_op_ids.clear()
    ow._active_op_tasks.clear()
    ow._accounts_in_use.clear()
    _m.reset()
    _m.set_shutting_down(False)
    ow._shutting_down = False


class _Pool:
    async def execute(self, q, *a):
        return "UPDATE 0"

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None


COUNTER = "infragram_executor_standdown_tasks_left_total"


def _snapshot_value(name: str) -> float:
    from services import metrics as _m
    snap = _m.snapshot().get("counters") or {}
    total = 0.0
    for key, val in snap.items():
        if key == name or key.startswith(name + "{"):
            total += float(val)
    return total


async def _stubborn(swallowed: list, hold: list) -> None:
    """Задача, которая отмену проглатывает — как уборка в `finally` с повторами.

    `hold[0] = False` снимает упрямство: иначе убрать задачу после проверки
    было бы нечем, и тест вис бы на своей же уборке.
    """
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            swallowed.append(1)
            if not hold or not hold[0] or len(swallowed) > 50:
                raise


async def _obedient(stopped: list) -> None:
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        stopped.append(1)
        raise


def _run(coro_factory):
    return asyncio.run(coro_factory())


# ── Главное: неостановленная задача больше не молчит ─────────────────────────

def test_a_task_that_ignored_the_cancel_is_counted(ow, monkeypatch, caplog):
    """Счётчик обязан сработать: иначе владелец об AUTH_KEY_DUPLICATED не узнает."""
    released: list = []

    async def _fake_release(ids):
        released.append(list(ids))

    monkeypatch.setattr(ow, "_db_release", _fake_release)
    swallowed: list = []
    hold = [True]

    async def _go():
        task = asyncio.create_task(_stubborn(swallowed, hold))
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(7)
        ow._accounts_in_use.add(99)
        with caplog.at_level(logging.ERROR, logger="services.op_worker"):
            res = await ow._stand_down_after_lease_loss(_Pool(), grace_s=0.1)
        hold[0] = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return res

    res = _run(_go)

    assert swallowed, "задача так и не получила отмену — тест не о том"
    assert _snapshot_value(COUNTER) >= 1, (
        "задача не свернулась, а счётчика нет: случай остался невидимым"
    )
    assert res["stopped"] == 0, f"нечего было останавливать: {res}"
    # Аренды всё равно снимаются: держать их нельзя, см. докстринг модуля.
    assert released == [[99]], f"аренды аккаунтов не сняты: {released}"
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.levelno >= logging.ERROR)
    assert "99" in text or "не свернул" in text or "не остановил" in text, (
        f"в логе нет ERROR про неостановленную задачу: {text!r}"
    )


def test_a_task_that_obeyed_the_cancel_counts_nothing(ow, monkeypatch):
    """Самопроверка мерки: на здоровой сдаче счётчик молчит.

    Счётчик, который срабатывает всегда, ничего не измеряет.
    """
    async def _fake_release(ids):
        return None

    monkeypatch.setattr(ow, "_db_release", _fake_release)
    stopped: list = []

    async def _go():
        task = asyncio.create_task(_obedient(stopped))
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(8)
        ow._accounts_in_use.add(77)
        return await ow._stand_down_after_lease_loss(_Pool(), grace_s=1.0)

    res = _run(_go)

    assert stopped, "задача не получила отмену — тест не о том"
    assert res["stopped"] == 1, f"свернувшаяся задача не сосчитана: {res}"
    assert _snapshot_value(COUNTER) == 0, (
        "счётчик сработал на здоровой сдаче аренды — мерка сломана"
    )


def test_the_stubborn_task_gets_a_second_bounded_chance(ow, monkeypatch):
    """Второй шанс есть, и он ограничен: цикл воркера нельзя держать вечно."""
    async def _fake_release(ids):
        return None

    monkeypatch.setattr(ow, "_db_release", _fake_release)
    swallowed: list = []
    hold = [True]

    async def _go():
        task = asyncio.create_task(_stubborn(swallowed, hold))
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(9)
        started = asyncio.get_running_loop().time()
        await ow._stand_down_after_lease_loss(_Pool(), grace_s=0.2)
        spent = asyncio.get_running_loop().time() - started
        hold[0] = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return spent

    spent = _run(_go)

    # Строго больше ДВУХ первых интервалов: «чуть больше 0.2» набежало бы и
    # от накладных расходов цикла, то есть такая мерка прошла бы и без фикса.
    assert spent > 0.5, (
        f"второго шанса не было: сдача уложилась в первый интервал ({spent:.2f}с)"
    )
    assert spent < 20.0, (
        f"сдача аренды заняла {spent:.1f}с — цикл воркера так держать нельзя"
    )


def test_the_counter_reaches_the_owner(ow):
    """Счётчик обязан быть в списке защитных: иначе он виден только на /metrics."""
    assert COUNTER in ow._PROTECTIVE_FAILURE_COUNTERS, (
        "счётчик не в _PROTECTIVE_FAILURE_COUNTERS — владелец о нём не узнает"
    )


# ── Стык со вторым механизмом: передача очереди — НЕ плановая остановка ──────

def test_a_handover_is_not_marked_as_a_planned_stop(ow, monkeypatch):
    """На этом различии держится вся громкость потерь при передаче очереди.

    `note_write_cancelled` молчит тревогой на плановой остановке (деплой на
    каждый пуш, оповещение на каждый выпуск перестанут читать) и поднимает её
    на всякой другой. Если сдача аренды начнёт отмечать себя плановой —
    например, кто-то «за компанию» поставит здесь `_shutting_down` — тревога
    умолкнет ровно в том случае, для которого её и завели. Поэтому отдельная
    проверка, а не доверие к тому, что флаг никто не поставит.
    """
    from services import metrics as _m

    async def _fake_release(ids):
        return None

    monkeypatch.setattr(ow, "_db_release", _fake_release)
    _m.set_shutting_down(False)

    async def _go():
        ow._accounts_in_use.add(42)
        await ow._stand_down_after_lease_loss(_Pool(), grace_s=0.1)

    _run(_go)

    assert _m._shutting_down is False, (
        "сдача аренды отметилась плановой остановкой — потери защитных записей "
        "при передаче очереди перестанут поднимать тревогу"
    )
    assert ow._shutting_down is False, (
        "сдача аренды поставила _shutting_down — процесс обязан уметь "
        "продолжить работу, аренду можно получить назад"
    )


def test_a_write_cancelled_by_the_handover_raises_the_alarm(ow, monkeypatch):
    """Сквозная проверка обещания: потеря при передаче очереди БУДИТ владельца.

    По отдельности это проверено (сдача гасит задачи; отменённая защитная
    запись считается и делится на плановую и внеплановую), а стык — нет. Стык
    и есть то, что обещано владельцу.
    """
    from services import metrics as _m
    UNPLANNED = "infragram_unplanned_write_cancel_failures_total"

    async def _fake_release(ids):
        return None

    monkeypatch.setattr(ow, "_db_release", _fake_release)

    class _SlowPool(_Pool):
        async def execute(self, q, *a):
            # Отмена задачи доходит до исполнителя именно здесь: уборка ждёт
            # базу, а база отвечает не мгновенно.
            await asyncio.sleep(5)
            return "UPDATE 1"

    wrote: list = []

    async def _worker():
        try:
            await asyncio.sleep(3600)
        finally:
            # Уборка исполнителя: отметка в журнале целей ПОСЛЕ доставки.
            wrote.append(await ow._journal_write(
                _SlowPool(), "INSERT INTO operation_log VALUES (1)",
                log_ctx="[op=11]", where="journal"))

    async def _go():
        task = asyncio.create_task(_worker())
        await asyncio.sleep(0)
        ow._active_op_tasks.add(task)
        ow._active_op_ids.add(11)
        await ow._stand_down_after_lease_loss(_Pool(), grace_s=0.1)
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    _run(_go)

    counters = (_m.snapshot() or {}).get("counters") or {}
    got = sum(float(v) for k, v in counters.items()
              if k == UNPLANNED or k.startswith(UNPLANNED + "{"))
    assert got >= 1, (
        "защитная запись потеряна при передаче очереди, а тревоги нет — "
        f"обещание владельцу не выполняется; счётчики: {sorted(counters)}"
    )
