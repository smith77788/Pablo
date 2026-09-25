"""Запросы strike-операции к Telegram ограничены по времени.

Мёртвый прокси чаще отдаёт не отказ, а half-open сокет: TCP установлен, коннект
прошёл, ответа нет и не будет. Такой запрос не возвращается НИКОГДА.

Почему именно здесь. Храповик `test_no_unbounded_telegram_request` вносит
`account_manager` в исключения («пройден по функциям пути операций»), а путь
strike идёт через него ДВУМЯ хопами: op_worker → strike_engine.staggered_strike →
account_manager.report_peer_deep_v2. В самом `report_peer_deep_v2` было 14
запросов без потолка, в `strike_map_target` — 6, то есть дыра в охране была
ровно там, где висящий запрос стоит дороже всего: массовая операция идёт циклом
последовательно, один повисший аккаунт останавливает её целиком, а она держит
слот параллельности (один из восьми) и арендованный флот часами. Владелец видит
«выполняется» с замершим счётчиком.
"""
from __future__ import annotations

import ast
import asyncio

import pytest

from services import account_manager as am


class _FakeClient:
    """Клиент, который умеет висеть, отвечать и отдавать не-корутины."""

    def __init__(self):
        self.calls: list[str] = []
        self._infragram_transport = "direct"

    async def get_entity(self, ref):
        self.calls.append("get_entity")
        return f"entity:{ref}"

    async def hangs_forever(self):
        await asyncio.Event().wait()          # никогда не вернётся

    async def __call__(self, request):
        self.calls.append(f"raw:{request}")
        if request == "hang":
            await asyncio.Event().wait()
        return f"ok:{request}"

    def iter_messages(self, *a, **kw):
        """Не корутина, а асинхронный итератор — оборачивать его нельзя."""
        async def _gen():
            yield "msg"
        return _gen()

    def _get_input_photo(self, photo):
        return f"input:{photo}"            # обычное значение, не корутина

    @property
    def session(self):
        return "сессия"


def _boxed(timeout=0.05):
    return am.timeboxed(_FakeClient(), timeout)


def test_a_hanging_request_stops_instead_of_hanging_forever():
    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await _boxed().hangs_forever()

    asyncio.run(asyncio.wait_for(go(), timeout=5))


def test_a_hanging_raw_request_stops_too():
    """Сырой TL-запрос `await client(ReportPeerRequest(...))` — их там 24."""
    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await _boxed()("hang")

    asyncio.run(asyncio.wait_for(go(), timeout=5))


def test_a_normal_request_passes_through_unchanged():
    assert asyncio.run(_boxed().get_entity("@chan")) == "entity:@chan"
    assert asyncio.run(_boxed()("ReportPeer")) == "ok:ReportPeer"


def test_an_async_iterator_is_not_wrapped():
    """iter_messages возвращает итератор, а не корутину: обёртка его сломала бы."""
    async def go():
        return [m async for m in _boxed().iter_messages()]

    assert asyncio.run(go()) == ["msg"]


def test_plain_methods_and_properties_pass_through():
    boxed = _boxed()
    assert boxed._get_input_photo("p1") == "input:p1"
    assert boxed.session == "сессия"
    assert boxed._infragram_transport == "direct"


def test_writing_an_attribute_reaches_the_real_client():
    real = _FakeClient()
    boxed = am.timeboxed(real, 1.0)
    boxed._infragram_transport = "bound"
    assert real._infragram_transport == "bound", (
        "запись ушла в обёртку, а не в клиент — проверки транспорта сломались бы")


def test_wrapping_twice_does_not_stack():
    once = am.timeboxed(_FakeClient(), 1.0)
    assert am.timeboxed(once, 1.0) is once


def test_default_timeout_is_generous():
    """Занижать нельзя: ложный таймаут уводит цель в повтор, то есть в лишнее
    действие по Telegram, а для strike это прямое давление к блокировке."""
    assert am._OP_TIMEOUT >= 30


def _fn_src(name: str) -> str:
    src = open(am.__file__, encoding="utf-8").read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


@pytest.mark.parametrize("fn", ["report_peer_deep_v2", "strike_map_target"])
def test_strike_functions_box_their_client(fn):
    body = _fn_src(fn)
    assert "client = timeboxed(client)" in body, (
        f"{fn} снова работает с клиентом без потолка на запрос")
    assert body.index("_make_client(") < body.index("client = timeboxed(client)"), (
        "обёртка ставится до создания клиента — часть вызовов останется голой")


@pytest.mark.parametrize("fn", ["report_peer_deep_v2", "strike_map_target"])
def test_only_one_client_is_created_per_function(fn):
    """Второй клиент, созданный ниже обёртки, вышел бы из-под потолка молча."""
    body = _fn_src(fn)
    assert body.count("_make_client(") == 1, (
        f"{fn} создаёт клиент больше одного раза — обёртка накрывает только "
        f"первый, остальные запросы снова без потолка")
