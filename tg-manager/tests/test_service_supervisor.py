"""Присмотр за фоновым сервисом не крутится вхолостую.

ЧТО ЛОМАЛОСЬ. Присмотр был вложенной функцией внутри main() и выглядел так:

    while True:
        try:
            await fn(*args)
        except Exception:
            ... sleep(30)

Все фоновые сервисы — бесконечные циклы, поэтому `fn` не возвращается никогда, и
ветки «вернулся штатно» как будто не существовало. Но три сервиса возвращаются
СРАЗУ и СИНХРОННО, если выключены настройкой: db_backup (DB_BACKUP_ENABLED=0 или
не задан чат назначения), account_rehab (INFRAGRAM_DISABLE_REHAB) и
payment_webhook (сервер вообще может завершиться).

Возврат без единого `await` означает, что корутина не отдала управление циклу
событий. `while True` тут же звал её снова — и снова, не уступая никому. Это не
«лишний перезапуск», это ЗАВИСАНИЕ ВСЕГО ПРОЦЕССА: вместе с выключенным бэкапом
встают бот, HTTP-API, мини-апп и воркер операций. Штатный выключатель одной
второстепенной подсистемы убивал продукт целиком.

Тест существует ещё и потому, что раньше эту ветку было НЕЧЕМ проверить: код
жил внутри main(). Модуль вынесен ровно для этого.
"""
from __future__ import annotations

import asyncio

import pytest

from services import service_supervisor


@pytest.mark.asyncio
async def test_service_disabled_by_config_is_not_called_in_a_tight_loop():
    """Ключевой случай: сервис возвращается синхронно, не сделав ни одного await."""
    calls = 0

    async def _disabled_service():
        nonlocal calls
        calls += 1
        return  # ни одного await — ровно как db_backup с выключенным бэкапом

    await asyncio.wait_for(
        service_supervisor.supervise("db_backup", _disabled_service), timeout=2
    )
    assert calls == 1, (
        f"выключенный сервис вызван {calls} раз(а) — присмотр крутится вхолостую "
        f"и не отдаёт управление циклу событий, то есть вешает весь процесс"
    )


@pytest.mark.asyncio
async def test_crashing_service_is_restarted_after_a_pause():
    calls = 0

    async def _flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("упал")
        return

    await asyncio.wait_for(
        service_supervisor.supervise("flaky", _flaky, restart_delay=0.01), timeout=2
    )
    assert calls == 3, "сервис не перезапускался после падения"


@pytest.mark.asyncio
async def test_network_service_is_restarted_on_a_normal_return_but_never_tightly():
    """Серверу положено подниматься снова — но через паузу, а не вхолостую."""
    calls = 0

    async def _server():
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise asyncio.CancelledError
        return

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            service_supervisor.supervise(
                "payment_webhook", _server, restart_delay=0.01, restart_on_return=True),
            timeout=2,
        )
    assert calls == 3


@pytest.mark.asyncio
async def test_cancellation_is_not_treated_as_a_crash():
    """Остановка процесса — не сбой: присмотр обязан пропустить отмену наверх."""
    async def _service():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service_supervisor.supervise("any", _service, restart_delay=0.01)


@pytest.mark.asyncio
async def test_start_delay_is_awaited_before_the_first_call():
    started = False

    async def _service():
        nonlocal started
        started = True

    task = asyncio.create_task(
        service_supervisor.supervise("any", _service, start_delay=0.2))
    await asyncio.sleep(0.05)
    assert not started, "разнос стартов не соблюдён — сервисы ударят в БД разом"
    await asyncio.wait_for(task, timeout=2)
    assert started


def test_main_uses_the_supervisor_instead_of_its_own_loop():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
        src = f.read()
    assert "service_supervisor.supervise(" in src
    assert "restart_on_return=False" in src, (
        "фоновые циклы перезапускаются после штатного возврата — выключенный "
        "настройкой сервис снова закрутится вхолостую"
    )
    assert "restart_on_return=True" in src, "сетевой сервис перестал подниматься снова"


def test_the_three_services_that_can_return_still_can():
    """Контроль смысла: если они перестанут возвращаться, тест выше потеряет силу.

    Проверяем не поведение (оно зависит от окружения), а наличие раннего выхода
    по настройке — того самого, на котором процесс и вставал.
    """
    import inspect

    from services import account_rehab, db_backup

    assert "return" in inspect.getsource(db_backup.run_backup_loop)
    assert "return" in inspect.getsource(account_rehab.run_rehab_loop)
