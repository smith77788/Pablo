"""Предохранитель разбирает ошибки Telegram по коду RPCError, а не поимённо.

Классификатор знал только точные имена классов. В telethon 1.36 у ServerError
(код 500) тридцать подклассов, а в списке транзиентных стояли четыре — значит
двадцать шесть «повтори запрос» (ChatGetFailedError, HistoryGetFailedError,
RandomIdDuplicateError, PersistentTimestampOutdatedError и прочие) считались
критичными и не ретраились ни разу: один сбой на стороне Telegram — и действие
падало, хотя повтор через секунду прошёл бы.

Вторая половина того же: семейство FloodError (код 420) — это не только
FloodWaitError. SlowModeWaitError, TakeoutInitDelayError и FloodTestPhoneWaitError
тоже несут .seconds и тоже означают «подожди столько-то». Узнавали же флуд по
имени, начинающемуся с Flood, поэтому ожидание слоу-мода при отправке в чат
выглядело фатальной ошибкой аккаунта.

Имена классов и коды здесь сверены с исходниками telethon 1.36.0
(telethon/errors/rpcbaseerrors.py, rpcerrorlist.py) — той версии, что закреплена
в requirements.txt.
"""
from __future__ import annotations

import asyncio

import pytest

from services.telethon_guard import (
    FloodHandoff, classify_telethon_error, guarded_call,
)


class _RPCError(Exception):
    """Форма telethon.errors.RPCError: у каждой ошибки есть .code."""

    code: int | None = None

    def __init__(self, seconds: int | None = None):
        super().__init__(type(self).__name__)
        if seconds is not None:
            self.seconds = seconds


# ── 500 INTERNAL: подклассы, которых не было в списке имён ────────────────────

class ChatGetFailedError(_RPCError):
    code = 500


class HistoryGetFailedError(_RPCError):
    code = 500


class RandomIdDuplicateError(_RPCError):
    code = 500


class PersistentTimestampOutdatedError(_RPCError):
    code = 500


class TimedoutError(_RPCError):      # именно так, со строчной «d» — код 503
    code = 503


# ── 420 FLOOD: ожидания, которые не начинаются со слова Flood ────────────────

class SlowModeWaitError(_RPCError):
    code = 420


class TakeoutInitDelayError(_RPCError):
    code = 420


# ── то, что обязано остаться критичным ───────────────────────────────────────

class PeerFloodError(_RPCError):     # 400 BAD_REQUEST, .seconds нет
    code = 400


class UserPrivacyRestrictedError(_RPCError):
    code = 400


class AuthKeyUnregisteredError(_RPCError):
    code = 406


@pytest.mark.parametrize("exc_cls", [
    ChatGetFailedError, HistoryGetFailedError, RandomIdDuplicateError,
    PersistentTimestampOutdatedError, TimedoutError,
])
def test_server_side_failures_are_retried(exc_cls):
    """500/503 — «повтори запрос», а не смерть действия."""
    assert classify_telethon_error(exc_cls()) == "transient", (
        f"{exc_cls.__name__} (код {exc_cls.code}) считается критичным — "
        "сбой на стороне Telegram обрывает действие вместо повтора"
    )


@pytest.mark.parametrize("exc_cls", [SlowModeWaitError, TakeoutInitDelayError])
def test_waits_without_flood_in_the_name_are_still_waits(exc_cls):
    """Код 420 + .seconds — это ожидание, как его ни зови."""
    assert classify_telethon_error(exc_cls(seconds=45)) == "flood", (
        f"{exc_cls.__name__} принят за фатальную ошибку — аккаунт спишут "
        "за обычное ожидание слоу-мода"
    )


def test_flood_family_without_seconds_is_not_invented_into_a_wait():
    """420 без .seconds — ждать нечего, выдумывать паузу нельзя."""
    class FloodError(_RPCError):
        code = 420

    assert classify_telethon_error(FloodError()) == "critical"


@pytest.mark.parametrize("exc_cls", [
    PeerFloodError, UserPrivacyRestrictedError, AuthKeyUnregisteredError,
])
def test_deliberate_critical_names_win_over_the_code_rule(exc_cls):
    """Спам-блок и мёртвая сессия остаются критичными — это решение по имени."""
    assert classify_telethon_error(exc_cls()) == "critical"


def test_negative_codes_are_recognised():
    """telethon прямо пишет: «Also witnessed as -500»."""
    class WeirdServerError(_RPCError):
        code = -500

    assert classify_telethon_error(WeirdServerError()) == "transient"


def test_garbage_code_does_not_break_classification():
    class Odd(Exception):
        code = "не число"

    class Boolish(Exception):
        code = True          # bool — не код, а случайный атрибут

    assert classify_telethon_error(Odd()) == "critical"
    assert classify_telethon_error(Boolish()) == "critical"


def test_unknown_error_is_still_critical():
    assert classify_telethon_error(ValueError("своё")) == "critical"


# ── поведение предохранителя ─────────────────────────────────────────────────

class _Client:
    def __init__(self):
        self.disconnected = 0

    async def disconnect(self):
        self.disconnected += 1


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(_):
        return None

    monkeypatch.setattr("services.telethon_guard.asyncio.sleep", _fast)


def test_server_failure_is_actually_retried_and_succeeds():
    client = _Client()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChatGetFailedError()
        return "готово"

    assert _run(guarded_call(client, factory, retries=2)) == "готово"
    assert calls["n"] == 2, "транзиентный сбой не был повторён"


def test_slow_mode_hands_off_with_its_real_wait():
    client = _Client()

    async def factory():
        raise SlowModeWaitError(seconds=45)

    with pytest.raises(FloodHandoff) as got:
        _run(guarded_call(client, factory))

    assert got.value.seconds == 45
    assert client.disconnected == 1, "перед handoff клиент обязан отключиться"


def test_cancellation_is_not_classified_as_an_error():
    """guarded_call ловит BaseException — отмена не должна попасть в разбор."""
    client = _Client()

    async def factory():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(guarded_call(client, factory))

    assert client.disconnected == 0
