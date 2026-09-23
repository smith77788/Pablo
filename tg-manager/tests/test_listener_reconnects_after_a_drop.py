"""Регрессия: слушатель, потерявший соединение, обязан переподключиться.

Единственным признаком «слушаем» была запись в `_listening`, а она переживает
обрыв соединения. Клиент создаётся с `connection_retries=1`: после неудачной
попытки Telethon сдаётся и остаётся отключённым навсегда.

Получалось худшее из двух сразу. Обновления не приходят — слушатель глухой, и
оператор об этом никак не узнаёт. При этом аккаунт по-прежнему числится
арендованным у арбитра, то есть выведен и из обычных операций. Один обрыв сети
— и аккаунт выпал из продукта до перезапуска процесса.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import audience_listener as al

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Client:
    def __init__(self, connected: bool):
        self._connected = connected
        self.disconnected = False

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self.disconnected = True
        self._connected = False


class _RudeClient(_Client):
    """Клиент, который на вопрос о соединении бросает исключение."""

    def is_connected(self):
        raise RuntimeError("транспорт закрыт")


@pytest.fixture(autouse=True)
def _clean():
    saved = dict(al._listening)
    al._listening.clear()
    yield
    al._listening.clear()
    al._listening.update(saved)


@pytest.fixture
def _released(monkeypatch):
    """Перехватывает возврат аренды арбитру.

    Патчим сам атрибут модуля: `from services import op_worker` берёт атрибут
    пакета, а не запись в sys.modules, поэтому подмена sys.modules мимо.
    """
    seen: list[int] = []

    async def _release(ids):
        seen.extend(int(i) for i in ids)

    async def _claim(acc_id):
        return True

    from services import op_worker as _opw

    monkeypatch.setattr(_opw, "release_accounts", _release)
    monkeypatch.setattr(_opw, "try_claim_account", _claim)
    return seen


def test_disconnected_listener_is_dropped(_released):
    dead = _Client(connected=False)
    al._listening[7] = dead

    asyncio.run(al._drop_disconnected())

    assert 7 not in al._listening, (
        "оборвавшийся слушатель остался в списке — переподключения не будет"
    )
    assert _released == [7], (
        f"аренда аккаунта не возвращена арбитру: {_released!r} — аккаунт выпал "
        "и из обычных операций"
    )


def test_connected_listener_is_kept(_released):
    alive = _Client(connected=True)
    al._listening[8] = alive

    asyncio.run(al._drop_disconnected())

    assert al._listening.get(8) is alive, "живой слушатель отключён без причины"
    assert alive.disconnected is False
    assert _released == []


def test_unanswerable_client_is_treated_as_dead(_released):
    al._listening[9] = _RudeClient(connected=True)

    asyncio.run(al._drop_disconnected())

    assert 9 not in al._listening, (
        "клиент, не ответивший о своём состоянии, оставлен как живой"
    )


def test_tick_reconnects_in_the_same_pass(monkeypatch, _released):
    """Тот же проход обязан подключить аккаунт заново, а не ждать следующего."""
    started: list[int] = []

    async def _desired(pool):
        return {7}

    async def _start(pool, bot, acc_id):
        started.append(acc_id)
        al._listening[acc_id] = _Client(connected=True)
        return True

    monkeypatch.setattr(al, "desired_accounts", _desired)
    monkeypatch.setattr(al, "_start_one", _start)

    al._listening[7] = _Client(connected=False)
    asyncio.run(al.tick(object(), object()))

    assert started == [7], (
        f"после обрыва аккаунт не переподключён в том же проходе: {started!r}"
    )


def test_tick_does_not_churn_a_healthy_listener(monkeypatch, _released):
    started: list[int] = []

    async def _desired(pool):
        return {7}

    async def _start(pool, bot, acc_id):
        started.append(acc_id)
        return True

    monkeypatch.setattr(al, "desired_accounts", _desired)
    monkeypatch.setattr(al, "_start_one", _start)

    alive = _Client(connected=True)
    al._listening[7] = alive
    asyncio.run(al.tick(object(), object()))

    assert started == [], "живой слушатель переподключён без нужды"
    assert alive.disconnected is False


def test_tick_checks_liveness():
    """Структурная страховка: проверка живости не должна пропасть из tick."""
    with open(os.path.join(ROOT, "services", "audience_listener.py"),
              encoding="utf-8") as f:
        src = f.read()
    import ast

    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "tick":
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "_drop_disconnected()" in body, (
                "tick перестал проверять, живы ли слушатели"
            )
            return
    pytest.fail("функция tick не найдена")
