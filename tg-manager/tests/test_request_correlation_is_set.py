"""Логи запроса и операции обязаны нести контекст корреляции (аудит №6).

Инфраструктура трассировки (services.logger: cid/user_id/op_id в contextvars,
формат добавляет их в каждую строку) существовала, но её выставлял ровно один
вход — апдейт-миддлварь бота. Мини-апп (главная веб-поверхность) и фоновые
операции логировали без контекста, и разбор аварии превращался в ручную склейку
строк по времени. Именно так владелец и присылал «логи запуска».

Здесь проверяется, что контекст ставится в НАЧАЛЕ обработки — до того, как
появится первая строка лога, которую нужно привязать.
"""
from __future__ import annotations

import ast
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from services.security import security_middleware
from services import logger as _logger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _reset_correlation():
    # Каждый тест начинает с пустого контекста, иначе прошлый cid маскирует баг.
    _logger._correlation_id.set("")
    _logger._user_id.set(None)
    _logger._op_id.set("")
    yield


@pytest.mark.asyncio
async def test_api_request_gets_a_correlation_id_before_handler_runs():
    seen = {}

    async def _handler(request):
        # ВАЖНО: читаем сырую contextvar, а НЕ correlation_id() — геттер
        # генерирует cid на лету, если пусто, и замаскировал бы то, что
        # миддлварь ничего не выставила. Нам нужно, чтобы значение уже стояло.
        seen["cid"] = _logger._correlation_id.get()
        return web.json_response({"ok": True})

    mw = security_middleware()
    req = make_mocked_request("GET", "/api/miniapp/whatever")
    await mw(req, _handler)

    assert seen.get("cid"), (
        "к моменту работы обработчика cid не выставлен миддлварью — строки лога "
        "запроса нечем связать")


@pytest.mark.asyncio
async def test_correlation_carries_user_when_authenticated(monkeypatch):
    """Если запрос авторизован — user_id обязан попасть в контекст."""
    import services.mini_app_api as api
    monkeypatch.setattr(api, "_get_uid", lambda request: 4242, raising=True)

    async def _handler(request):
        return web.json_response({"ok": True})

    mw = security_middleware()
    req = make_mocked_request(
        "GET", "/api/miniapp/x", headers={"Authorization": "Bearer tok"})
    await mw(req, _handler)

    assert _logger._user_id.get() == 4242, (
        "user_id не попал в контекст — ошибку в логе не привязать к человеку")


def _func_node(path: str, name: str):
    src = open(path, encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n, src
    raise AssertionError(f"{name} не найдена в {path}")


def test_op_worker_sets_correlation_at_the_start():
    """Операция ставит контекст до основной работы, иначе ранние логи без cid."""
    path = os.path.join(ROOT, "services", "op_worker.py")
    node, src = _func_node(path, "_run_op_task")
    lines = src.split("\n")
    body = "\n".join(lines[node.lineno - 1:node.end_lineno])

    assert "set_correlation_id" in body, (
        "_run_op_task не выставляет корреляцию — логи операции без контекста")
    # до фактического исполнения: раньше метрики длительности и раньше циклов.
    i_set = body.index("set_correlation_id")
    i_started = body.index("_t_started = time.monotonic()")
    assert i_set < i_started, (
        "корреляция ставится после начала работы операции — ранние строки "
        "лога остаются без cid/op_id")


def test_op_worker_correlation_includes_op_and_user():
    """В контексте операции должны быть и op_id, и user_id — иначе не сгруппировать."""
    path = os.path.join(ROOT, "services", "op_worker.py")
    node, src = _func_node(path, "_run_op_task")
    lines = src.split("\n")
    body = "\n".join(lines[node.lineno - 1:node.end_lineno])
    # аргументы вызова set_correlation_id(...)
    call = body[body.index("set_correlation_id"):]
    assert "op_id=" in call, "op_id не передан в корреляцию операции"
    assert "user_id=" in call, "user_id не передан в корреляцию операции"
