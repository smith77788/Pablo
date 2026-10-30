"""Ослепший детектор остановки обязан сказать об этом, а не промолчать.

МЕХАНИКА. `operation_bus.queue_frozen` отвечает на единственный системный
вопрос продукта: «исполнитель операций встал, а готовая работа ждёт». Severity у
этой аномалии critical, и не зря: пока это длится, не исполняется НИ ОДНА
операция, а на экране владельца всё выглядит обычным «ожидает».

РАЗРЫВ. Свой провал детектор съедал в `log.debug` и возвращал `None` — то есть
«всё в порядке». Ослепший запрос становился неотличим от здоровой очереди:
ни в логе рабочего уровня, ни на графике, ни в тревоге владельцу. Ровно тот
класс, из-за которого защиту называют молча отключённой: сломался не продукт, а
глаз, которым за ним смотрят, и сломался он беззвучно.

Пример, когда это НЕ равно «база недоступна, всё равно всё лежит»: ошибка типа
в запросе после правки схемы, снятая колонка, `command_timeout` на загруженной
базе. Продукт в эти моменты работает, очередь реально может встать — и сказать
об этом некому.

ЧТО ЗАКРЕПЛЕНО. На провале проверки: предупреждение в лог (не debug) и счётчик
`infragram_queue_frozen_check_failures_total`, который входит в набор тревог
владельцу (`op_worker._PROTECTIVE_FAILURE_COUNTERS`) — единственный способ, каким
«защита перестала работать» до владельца доходит. Ответ при этом остаётся
`None`: ронять дашборд и разбор аномалий из-за своей слепоты детектор не должен.
"""

import logging

import pytest

COUNTER = "infragram_queue_frozen_check_failures_total"


class _BlindPool:
    """Пул, у которого запрос падает — именно так детектор и слепнет."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc or RuntimeError("column \"wait_s\" does not exist")

    async def fetch(self, *a, **k):
        raise self.exc

    async def fetchval(self, *a, **k):
        raise self.exc

    async def fetchrow(self, *a, **k):
        raise self.exc

    async def execute(self, *a, **k):
        raise self.exc


def _count(name: str = COUNTER) -> float:
    from services import metrics

    return float(metrics.snapshot()["counters"].get(name, 0))


@pytest.mark.asyncio
async def test_the_detector_counts_its_own_blindness(caplog):
    from services import metrics, operation_bus

    metrics.reset()
    with caplog.at_level(logging.WARNING, logger="services.operation_bus"):
        verdict = await operation_bus.queue_frozen(_BlindPool())

    assert verdict is None, (
        "детектор обязан отвечать None и не ронять вызывающего — его зовут из "
        "дашборда владельца на каждый запрос")
    assert _count() == 1, (
        "слепота детектора не попала на график: снаружи сломанный запрос "
        f"неотличим от здоровой очереди ({metrics.snapshot()['counters']})")
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "провал проверки остался на уровне debug — в рабочих логах его нет")


@pytest.mark.asyncio
async def test_the_anomaly_side_counts_it_too(caplog):
    """Второй глаз: сборка аномалии тоже может упасть, и тоже молча."""
    from services import anomaly_detector, metrics

    metrics.reset()
    with caplog.at_level(logging.WARNING, logger="services.anomaly_detector"):
        out = await anomaly_detector._detect_queue_frozen(_BlindPool())

    assert out == [], "разбор аномалий не должен падать из-за своей слепоты"
    assert _count() == 1, "слепота разбора аномалий не попала на график"
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_the_counter_is_an_owner_alarm():
    """Счётчик без тревоги растёт в тишине: владелец графиков не смотрит."""
    import ast
    import pathlib

    src = pathlib.Path("services/op_worker.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        targets = (node.targets if isinstance(node, ast.Assign) else [])
        if any(getattr(t, "id", "") == "_PROTECTIVE_FAILURE_COUNTERS"
               for t in targets):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)}
            assert COUNTER in keys, (
                "счётчик слепоты не в наборе тревог владельцу — он будет "
                "расти, и никто не узнает")
            return
    raise AssertionError("набор тревог владельцу не найден")
