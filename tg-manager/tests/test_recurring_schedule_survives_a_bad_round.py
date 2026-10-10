"""Один неудачный круг не убивает расписание навсегда — и не делает это молча.

ЧТО БЫЛО. Продление повторяющейся операции стояло под условием «прогон был
продуктивным». Один неудачный круг обрывал расписание НАВСЕГДА: автопостинг,
заведённый на месяц, умирал от любой случайности — сеть моргнула, весь флот на
полчаса ушёл в карантин, Telegram ответил ошибкой на все цели сразу.

Владелец об этом не узнавал. Про две другие причины обрыва (предохранитель,
тариф) ему говорят отдельным сообщением; про «круг не удался» не говорили
ничего. Отказ выглядел так: канал перестал наполняться, а через неделю владелец
случайно это замечает. Ровно тот же разрыв уже закрывали для 'partial' — один
сбойный канал в серии не должен обрывать серию; для 'failed' остановились на
полпути.

Аварийный исход (исключение, повторы исчерпаны) не знал про продление вообще.

ЧТО ТЕПЕРЬ. Неудачный круг расписание переживает, но не бесконечно: считаем
неудачи ПОДРЯД и на пределе останавливаемся с объяснением. Успешный круг
обнуляет счётчик. Следующий круг по-прежнему ставится через шину, поэтому
предохранитель и гейт тарифа его так же держат — повтор после провала не
обходит ни одну защиту.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"

_OP = "quick_post"


class _Pool:
    async def fetchrow(self, query, *args):
        return {"label": "Пост", "total_items": 10}

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


class _Bot:
    pass


@pytest.fixture
def bus(monkeypatch):
    """Перехватываем постановку следующего круга и уведомления владельцу."""
    from services import op_worker
    from services import operation_bus

    submitted: list[dict] = []
    notified: list[str] = []

    async def _submit(pool, owner_id, op_type, params, **kw):
        submitted.append({"op_type": op_type, "params": params, **kw})
        return 999

    async def _notify(pool, bot, owner_id, pref, text, **kw):
        notified.append(text)

    monkeypatch.setattr(operation_bus, "submit", _submit)
    monkeypatch.setattr(op_worker.db, "notify_if_enabled", _notify)
    return submitted, notified


async def _run(params: dict, *, productive: bool, why: str = "сеть недоступна"):
    from services import op_worker

    await op_worker._reschedule_recurring(
        _Pool(), _Bot(), 7, 555, _OP, params, productive=productive, why=why,
    )


@pytest.mark.asyncio
async def test_failed_round_still_schedules_the_next_one(bus):
    submitted, notified = bus
    await _run({"repeat_interval_min": 60}, productive=False)

    assert submitted, (
        "один неудачный круг оборвал расписание навсегда — канал просто "
        "перестаёт наполняться, и владелец узнаёт об этом через неделю"
    )
    assert not notified, "расписание живо, останавливать владельца нечем"


@pytest.mark.asyncio
async def test_failure_streak_is_carried_into_the_next_round(bus):
    from services import op_worker

    submitted, _ = bus
    await _run({"repeat_interval_min": 60}, productive=False)

    key = op_worker._RECURRING_STREAK_KEY
    assert submitted[0]["params"].get(key) == 1, (
        "счётчик неудач не поехал в следующий круг — серия провалов стала бы вечной"
    )


@pytest.mark.asyncio
async def test_successful_round_resets_the_streak(bus):
    from services import op_worker

    submitted, _ = bus
    key = op_worker._RECURRING_STREAK_KEY
    await _run({"repeat_interval_min": 60, key: 2}, productive=True)

    assert key not in submitted[0]["params"], (
        "удачный круг не обнулил счётчик: две разнесённые во времени неудачи "
        "копились бы до остановки исправного расписания"
    )


@pytest.mark.asyncio
async def test_streak_limit_stops_the_schedule_and_says_so(bus):
    from services import op_worker

    submitted, notified = bus
    key = op_worker._RECURRING_STREAK_KEY
    await _run(
        {"repeat_interval_min": 60, key: op_worker._RECURRING_MAX_FAIL_STREAK - 1},
        productive=False,
    )

    assert not submitted, "расписание, которое не работает подряд, крутится вечно"
    assert notified, "расписание остановлено молча — худший вид отказа"
    text = notified[0]
    assert "#7" in text and "остановлен" in text


@pytest.mark.asyncio
async def test_failed_round_does_not_spend_repeat_count(bus):
    submitted, _ = bus
    await _run({"repeat_interval_min": 60, "repeat_count": 5}, productive=False)

    assert submitted[0]["params"]["repeat_count"] == 5, (
        "«опубликовать 5 раз» означает пять публикаций, а не пять попыток"
    )


@pytest.mark.asyncio
async def test_successful_round_spends_repeat_count(bus):
    submitted, _ = bus
    await _run({"repeat_interval_min": 60, "repeat_count": 5}, productive=True)

    assert submitted[0]["params"]["repeat_count"] == 4


@pytest.mark.asyncio
async def test_last_scheduled_round_ends_the_chain(bus):
    submitted, notified = bus
    await _run({"repeat_interval_min": 60, "repeat_count": 1}, productive=True)

    assert not submitted, "серия «5 раз» не должна продолжаться после пятого раза"
    assert not notified, "штатное окончание серии — не повод для тревожного сообщения"


@pytest.mark.asyncio
async def test_one_off_operation_is_not_rescheduled(bus):
    submitted, notified = bus
    await _run({}, productive=False)
    await _run({"repeat_interval_min": 0}, productive=False)

    assert not submitted and not notified


@pytest.mark.asyncio
async def test_account_actions_are_never_rescheduled(bus, monkeypatch):
    """Продлеваются только постинг-операции: аккаунт-действия зациклить нельзя."""
    from services import op_worker

    submitted, _ = bus
    await op_worker._reschedule_recurring(
        _Pool(), _Bot(), 7, 555, "mass_join", {"repeat_interval_min": 60},
        productive=False,
    )
    assert not submitted


@pytest.mark.asyncio
async def test_circuit_breaker_still_stops_the_chain_with_an_explanation(bus, monkeypatch):
    """Повтор после провала не обходит предохранитель — он идёт через шину."""
    from services import operation_bus

    submitted, notified = bus

    async def _blocked(pool, owner_id, op_type, params, **kw):
        raise operation_bus.ImmunityBlockedError(op_type, "тип операции приостановлен")

    monkeypatch.setattr(operation_bus, "submit", _blocked)
    await _run({"repeat_interval_min": 60}, productive=False)

    assert notified and "предохранитель" in notified[0]


def test_crashed_recurring_operation_also_gets_its_next_round():
    """Аварийный исход обязан продлевать расписание так же, как обычный."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    run_op = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_op_task"
    )
    handlers = [
        h for n in ast.walk(run_op) if isinstance(n, ast.Try) for h in n.handlers
    ]
    calls_in_handlers = {
        c.func.id
        for h in handlers for c in ast.walk(h)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "_reschedule_recurring" in calls_in_handlers, (
        "операция с расписанием, упавшая с исключением, обрывает автопостинг "
        "молча и навсегда — аварийный путь не знает про продление"
    )
