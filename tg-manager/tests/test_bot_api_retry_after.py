"""Bot API: пауза по 429 не вешает вызывающего, а поднимается наверх.

Telegram отдаёт ботам `parameters.retry_after` и в сотни секунд — на рассылке по
многим чатам, на лимите бота целиком. Клиент спал ровно столько, сколько его
попросили, и делал так до трёх раз подряд: рассылка, воронка и релей оператору
останавливались на всё это время, хотя единственное, что требовалось, — отдать
настоящий retry_after наверх и дать вызывающему перенести задачу.

Здесь же второй класс: Telegram присылает `"parameters": null` наравне с
объектом, а код читал `data.get("parameters", {}).get(...)` — default у .get()
срабатывает только на ОТСУТСТВУЮЩЕМ ключе, поэтому на явном null это падало
AttributeError внутри ретрай-цикла.
"""
from __future__ import annotations

import asyncio

import pytest

from services import bot_api


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Отдаёт заготовленные ответы по порядку и считает запросы."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        payload = self._payloads[min(self.calls - 1, len(self._payloads) - 1)]
        code = payload.get("error_code") if isinstance(payload, dict) else 502
        return _FakeResponse(payload, code or 200)


def _flood(retry_after):
    return {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": retry_after},
    }


def _run_tracking_sleeps(coro_factory):
    """Выполнить корутину, подменив asyncio.sleep счётчиком."""
    slept: list[float] = []

    async def _fake_sleep(sec):
        slept.append(sec)

    real_sleep = asyncio.sleep
    asyncio.sleep = _fake_sleep
    try:
        return asyncio.run(coro_factory()), slept
    finally:
        asyncio.sleep = real_sleep


def test_long_retry_after_is_not_slept_and_is_returned():
    """Длинная пауза: ни одного сна, наверх уходит настоящий retry_after."""
    long_wait = bot_api._MAX_INLINE_RETRY_AFTER_S + 300
    session = _FakeSession(_flood(long_wait))

    data, slept = _run_tracking_sleeps(
        lambda: bot_api._call(session, "t0ken", "sendMessage", chat_id=1, text="x")
    )

    assert not slept, f"клиент уснул на {slept} вместо того, чтобы отдать паузу наверх"
    assert session.calls == 1, "длинную паузу нельзя проретраить — это те же часы"
    assert bot_api.retry_after_of(data) == long_wait, (
        "вызывающий обязан получить НАСТОЯЩУЮ паузу, иначе он перенесёт задачу не туда"
    )


def test_short_retry_after_is_still_slept_and_retried():
    """Короткую паузу дешевле переждать на месте — поведение не меняем."""
    short_wait = max(1, bot_api._MAX_INLINE_RETRY_AFTER_S - 5)
    session = _FakeSession(_flood(short_wait), {"ok": True, "result": {"message_id": 7}})

    data, slept = _run_tracking_sleeps(
        lambda: bot_api._call(session, "t0ken", "sendMessage", chat_id=1, text="x")
    )

    assert slept == [short_wait]
    assert session.calls == 2
    assert data["ok"] is True


def test_null_parameters_does_not_crash():
    """`"parameters": null` — валидный ответ Telegram, а не повод падать."""
    session = _FakeSession(
        {"ok": False, "error_code": 429, "description": "Too Many Requests",
         "parameters": None},
        {"ok": True, "result": {"message_id": 1}},
    )

    data, slept = _run_tracking_sleeps(
        lambda: bot_api._call(session, "t0ken", "sendMessage", chat_id=1, text="x")
    )

    assert data["ok"] is True, "ответ с parameters=null не должен ронять ретрай-цикл"
    assert slept == [5], "без retry_after берётся дефолт, а не исключение"


def test_retry_after_of_survives_garbage():
    assert bot_api.retry_after_of(None) is None
    assert bot_api.retry_after_of({}) is None
    assert bot_api.retry_after_of({"parameters": None}) is None
    assert bot_api.retry_after_of({"parameters": []}) is None
    assert bot_api.retry_after_of({"parameters": {"retry_after": "нет"}}) is None
    assert bot_api.retry_after_of({"parameters": {"retry_after": "42"}}) == 42


def test_non_json_object_answer_is_not_an_attributeerror():
    """Шлюз вместо Telegram вернул список — дальше по коду везде .get()."""
    session = _FakeSession()
    session._payloads = [["не", "объект"]]

    data, _ = _run_tracking_sleeps(
        lambda: bot_api._call(session, "t0ken", "getMe")
    )

    assert data["ok"] is False
    assert "Неожиданный ответ" in data["description"]


@pytest.mark.parametrize("sender", ["send_message", "send_message_classified"])
def test_senders_report_the_real_retry_after(sender):
    """Рассылка переносит задачу по числу из ответа — оно должно быть настоящим."""
    long_wait = bot_api._MAX_INLINE_RETRY_AFTER_S + 120
    session = _FakeSession(_flood(long_wait))
    fn = getattr(bot_api, sender)

    out, slept = _run_tracking_sleeps(
        lambda: fn(session, "t0ken", 42, "привет")
    )

    assert not slept
    assert out[0] is False
    assert out[1] == long_wait
