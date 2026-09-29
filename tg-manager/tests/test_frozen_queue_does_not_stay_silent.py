"""Остановленный исполнитель операций больше не молчит.

ЧТО ЛОМАЛОСЬ. Разбор очереди живёт в одном цикле — `op_worker.run`. Присмотр за
ним (`service_supervisor.supervise`) при падении пишет строку в лог и через 30
секунд пробует снова, и так вечно. Если падение повторяемое, продукт попадает в
состояние, которое снаружи не отличить от порядка: процесс жив, бот отвечает,
мини-апп открывается, хартбит процесса (`process_heartbeats`) исправно
обновляется ОТДЕЛЬНЫМ сервисом — а очередь не разбирается вообще. Владелец видит
у каждой своей операции ровно «ожидает»: без причины и без срока.

Заметить это было некому. Сторож воркера (`_watchdog_alerts`) живёт внутри того
же цикла и умирает вместе с ним, а вне `op_worker` факт исполнения не проверял
никто: `account_monitor`, `infra_advisor`, `anomaly_detector` и `fleet_doctor`
считали количества и возраст строк, но не то, разбирает ли их кто-нибудь.

Вторая половина находки — почему цикл вообще мог завершиться. Четыре вызова
обслуживания (продление аренд, сторож зависших, реконсилер флагов, алерт о
застрявших) стояли ЗА try. Исключение из любого из них не костило один круг, а
завершало `run`; присмотр звал `run` заново, а её первое действие —
`_reset_stale_running`, возвращающий в очередь ВСЁ, что висит в 'running', со
списанием единицы бюджета живучести каждой операции. Задачи операций при этом
живут отдельно (`create_task`), то есть перезапуск бил по идущей работе:
прогресс обнулялся, операция показывалась как «ожидает», а через `_MAX_REVIVES`
таких перезапусков здоровая многочасовая рассылка объявлялась ядовитой и падала
в 'failed'.

ЧТО ТЕПЕРЬ. Поллер отмечается в platform_settings каждый ~30-й секундой круга;
`operation_bus.queue_frozen` сравнивает возраст отметки с возрастом ГОТОВОЙ к
запуску работы и отвечает «очередь не разбирается» только когда верно и то, и
другое. Читают его два независимых от воркера места: детектор аномалий (шлёт
владельцу) и карточка «требует внимания» в мини-аппе. Обслуживание цикла
целиком под try.
"""
from __future__ import annotations

import ast
import asyncio
import inspect

import pytest


# ── Заглушка пула ────────────────────────────────────────────────────────────


class _Pool:
    """Пул, отдающий заранее заданные строки; помнит SQL и параметры."""

    def __init__(self, rows=None, raises: bool = False):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self.fetched: list[tuple] = []
        self.executed: list[tuple] = []

    async def fetch(self, sql, *args):
        if self._raises:
            raise RuntimeError("БД недоступна")
        self.fetched.append((sql, args))
        return list(self._rows)

    async def execute(self, sql, *args):
        if self._raises:
            raise RuntimeError("БД недоступна")
        self.executed.append((sql, args))
        return "UPDATE 1"


def _row(owner_id: int, cnt: int, wait_s: float, silence_s):
    return {"owner_id": owner_id, "cnt": cnt, "wait_s": wait_s, "silence_s": silence_s}


# ── Признак жизни поллера ────────────────────────────────────────────────────


def test_the_poller_leaves_a_mark():
    from services import operation_bus

    pool = _Pool()
    asyncio.run(operation_bus.mark_poller_alive(pool, "host:1:abc"))

    assert pool.executed, "поллер не оставил признака жизни"
    sql, args = pool.executed[0]
    assert "platform_settings" in sql and "ON CONFLICT" in sql, (
        "отметка должна быть UPSERT по одному ключу, иначе вторая запись упадёт"
    )
    assert operation_bus.POLLER_MARK_KEY in args
    assert "updated_at = now()" in sql, (
        "возраст отметки обязан считаться по часам БД: часы процесса могут "
        "разъехаться, и тогда детектор соврёт в любую сторону"
    )


def test_the_mark_never_breaks_the_loop_it_describes():
    from services import operation_bus

    # Признак жизни не имеет права уронить цикл, чью жизнь он описывает.
    asyncio.run(operation_bus.mark_poller_alive(_Pool(raises=True), "x"))


# ── Детектор «очередь не разбирается» ────────────────────────────────────────


def test_a_fresh_mark_means_the_queue_is_fine():
    from services import operation_bus

    # Работа ждёт, но поллер отметился только что — это нормальная очередь
    # (потолок параллельности, лимит на владельца), а не остановка.
    pool = _Pool([_row(7, 5, 3600, 12.0)])
    assert asyncio.run(operation_bus.queue_frozen(pool)) is None


def test_silence_with_work_waiting_is_reported():
    from services import operation_bus

    pool = _Pool([
        _row(7, 3, 1800, 900.0),
        _row(9, 2, 600, 900.0),
    ])
    verdict = asyncio.run(operation_bus.queue_frozen(pool))

    assert verdict is not None, "очередь стоит 15 минут, а детектор молчит"
    assert verdict["pending"] == 5
    assert verdict["owners"] == {7: 3, 9: 2}, (
        "сообщить надо каждому владельцу, чья работа стоит"
    )
    assert verdict["oldest_min"] == 30


def test_silence_without_work_is_not_an_incident():
    from services import operation_bus

    # Пустая очередь: мёртвый исполнитель пока никому не мешает, а сообщение о
    # нём было бы шумом на каждом деплое.
    assert asyncio.run(operation_bus.queue_frozen(_Pool([]))) is None


def test_a_poller_that_never_started_is_not_invisible():
    from services import operation_bus

    # Отметки нет вовсе: неверная роль процесса, падение на первой строке run.
    # Трактовать это как «неизвестно» значит не увидеть аварию никогда.
    verdict = asyncio.run(operation_bus.queue_frozen(_Pool([_row(7, 4, 1200, None)])))

    assert verdict is not None
    assert verdict["silence_s"] is None


def test_a_broken_detector_does_not_invent_an_outage():
    from services import operation_bus

    assert asyncio.run(operation_bus.queue_frozen(_Pool(raises=True))) is None


def test_waiting_by_the_clock_is_not_a_stall():
    from services import operation_bus

    src = inspect.getsource(operation_bus.queue_frozen)
    # Отложенная операция (флуд-пауза, «продолжим завтра», повтор после
    # cooldown) ждёт своего срока и остановкой не считается; операция на
    # подтверждении ждёт владельца. Условия — те же, по которым берёт поллер.
    assert "scheduled_for" in src and "requires_approval IS NOT TRUE" in src, (
        "детектор считает остановкой законное ожидание срока или подтверждения"
    )


# ── Цикл воркера ─────────────────────────────────────────────────────────────


def _run_body() -> ast.AST:
    from services import op_worker

    tree = ast.parse(inspect.getsource(op_worker.run))
    return tree.body[0]


_MAINTENANCE = ("renew_leases", "_watchdog_stale", "_reconcile_in_operation",
                "_watchdog_alerts", "mark_poller_alive")


def test_the_poll_loop_marks_itself_alive():
    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(_run_body()) if isinstance(n, ast.Call)
    }
    assert "mark_poller_alive" in calls, (
        "цикл исполнителя не оставляет признака жизни — снаружи его остановку "
        "не отличить от пустой очереди"
    )


@pytest.mark.parametrize("name", _MAINTENANCE)
def test_maintenance_failure_costs_one_cycle_not_the_worker(name: str):
    """Каждый вызов обслуживания обязан стоять ВНУТРИ try.

    Иначе его сбой завершает `run`, присмотр запускает её заново, и
    `_reset_stale_running` списывает бюджет живучести живым операциям.
    """
    body = _run_body()
    guarded: set[int] = set()
    for node in ast.walk(body):
        if isinstance(node, ast.Try):
            for inner in node.body:
                for n in ast.walk(inner):
                    if isinstance(n, ast.Call):
                        guarded.add(id(n))

    found = False
    for node in ast.walk(body):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if fname != name:
            continue
        found = True
        assert id(node) in guarded, (
            f"{name}() вызывается вне try: его сбой завершит цикл воркера, а не "
            f"один круг"
        )
    assert found, f"{name}() пропал из цикла воркера"


def test_a_cancelled_worker_still_stops():
    """Отмена остаётся отменой: остановка процесса — не сбой цикла."""
    from services import op_worker

    src = inspect.getsource(op_worker.run)
    assert src.count("except asyncio.CancelledError:") == 2, (
        "у защищённых участков цикла должна быть своя ветка отмены, иначе "
        "SIGTERM превращается в 'op_worker error' и цикл продолжает крутиться"
    )


# ── Кто об этом сообщает ─────────────────────────────────────────────────────


def test_the_anomaly_is_raised_for_every_affected_owner(monkeypatch):
    from services import anomaly_detector, operation_bus

    async def _frozen(pool, silence_s: int = 0):
        return {"silence_s": 900.0, "pending": 5, "oldest_min": 30,
                "owners": {7: 3, 9: 2}}

    monkeypatch.setattr(operation_bus, "queue_frozen", _frozen)
    found = asyncio.run(anomaly_detector._detect_queue_frozen(_Pool()))

    assert {a.owner_id for a in found} == {7, 9}
    assert all(a.anomaly_type == "queue_frozen" for a in found)
    assert all(a.severity == "critical" for a in found), (
        "пока это длится, не исполняется ни одна операция продукта"
    )
    assert all(a.detector == "queue" for a in found)
    # Владелец должен понять из текста, что работа не потеряна: иначе он начнёт
    # отменять и ставить заново, наращивая очередь, которую некому разобрать.
    assert any("не потеряна" in a.description for a in found)


def test_no_anomaly_when_the_queue_is_healthy(monkeypatch):
    from services import anomaly_detector, operation_bus

    async def _ok(pool, silence_s: int = 0):
        return None

    monkeypatch.setattr(operation_bus, "queue_frozen", _ok)
    assert asyncio.run(anomaly_detector._detect_queue_frozen(_Pool())) == []


def test_the_global_check_runs_even_without_owners():
    """Проверка не по владельцу, и цикл по владельцам ей не указ."""
    from services import anomaly_detector

    src = inspect.getsource(anomaly_detector.run_full_detection)
    head, _, tail = src.partition("for row in owner_rows:")
    assert tail, "цикл по владельцам не найден"
    assert "_detect_queue_frozen" in head, (
        "глобальная проверка стоит внутри или после разбора по владельцам — при "
        "пустом списке владельцев остановку продукта никто не заметит"
    )


def test_the_miniapp_shows_the_frozen_queue():
    from services import mini_app_api

    src = inspect.getsource(mini_app_api)
    assert 'add("ops_frozen"' in src, (
        "мини-апп по-прежнему показывает только «ожидает» — владелец не может "
        "отличить нормальную очередь от остановленного исполнителя"
    )
    start = src.index('# ── Очередь вообще не разбирается')
    end = src.index('# ── Операции, застрявшие в работе', start)
    card = src[start:end]
    assert "queue_frozen" in card
    assert "endpoint" not in card, (
        "у карточки появилась кнопка: изнутри мини-аппа исполнителя не поднять, "
        "а «поставить очередь на паузу» здесь было бы прямо во вред"
    )
