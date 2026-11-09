"""Пауза Telegram соблюдается на входе, а не по памяти вызывающего.

ЧТО БЫЛО. Записать паузу (FloodWait) в пульс флота обязан был тот, кто вызвал
действие. Вызывающих больше сотни, и в 53 файлах, работающих аккаунтами
(фабрики каналов и групп, SEO, чистка, витрины, консоль ИИ), записи не было
вовсе. Telegram говорил «ждите 10 минут», экран показывал ошибку, а подбор
аккаунтов считал аккаунт отдохнувшим и отдавал его следующей операции прямо под
действующее ограничение. Повтор под паузой Telegram читает как упорство:
пауза растёт, затем аккаунт ограничивают.

ЧТО ТЕПЕРЬ. Клиент аккаунта сам отмечает паузу на любом запросе и не
отправляет тот же вид запроса раньше срока.
"""
from __future__ import annotations

import asyncio

import pytest



def _pause(name: str, seconds: int) -> Exception:
    import telethon.errors as errors
    err = getattr(errors, name)(f"A wait of {seconds} seconds is required")
    err.seconds = seconds
    return err


class SendMessageRequest:  # имя вида запроса — ключ порога
    pass


class GetDialogsRequest:
    pass


class _Client:
    """Клиент, у которого Telegram на первый запрос отвечает паузой."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent: list[str] = []

    async def _call(self, sender, request, ordered=False):
        self.sent.append(type(request).__name__)
        ans = self.answers.pop(0) if self.answers else "ok"
        if isinstance(ans, Exception):
            raise ans
        return ans


@pytest.fixture
def fe():
    from services import flood_engine
    saved = (dict(flood_engine._request_floor), dict(flood_engine._floor_unsaved),
             dict(flood_engine._flood_state))
    flood_engine._request_floor.clear()
    flood_engine._floor_unsaved.clear()
    yield flood_engine
    for live, old in zip((flood_engine._request_floor, flood_engine._floor_unsaved,
                          flood_engine._flood_state), saved):
        live.clear()
        live.update(old)


def _wrapped(answers, account_id=7001):
    from services import account_manager as am
    c = _Client(answers)
    return am._wrap_client_flood_floor(c, account_id), c


def test_a_pause_nobody_recorded_still_cools_the_account(fe):
    from telethon.errors import FloodWaitError
    client, raw = _wrapped([_pause("FloodWaitError", 600)])
    with pytest.raises(FloodWaitError):
        asyncio.run(client._call(None, SendMessageRequest()))
    # Вызывающий ничего не записал — а подбор аккаунтов уже знает о паузе.
    assert fe.is_account_cooling(7001)
    assert 590 < fe.seconds_until_ready(7001) <= 600
    assert fe.drain_floor_unsaved()[0][0] == 7001, "пауза должна дойти до базы"


def test_the_same_request_does_not_reach_telegram_during_the_pause(fe):
    from telethon.errors import FloodWaitError
    client, raw = _wrapped([_pause("FloodWaitError", 600), "ok", "ok"])
    with pytest.raises(FloodWaitError):
        asyncio.run(client._call(None, SendMessageRequest()))
    # Новый клиент той же сессии (следующая операция) — память Telethon пуста.
    from services import account_manager as am
    again = am._wrap_client_flood_floor(_Client(["ok"]), 7001)
    with pytest.raises(FloodWaitError) as err:
        asyncio.run(again._call(None, SendMessageRequest()))
    assert again.sent == [], "запрос под действующей паузой ушёл в Telegram"
    assert 590 < err.value.seconds <= 601
    # Другой вид запроса и другой аккаунт под паузу не попадают.
    assert asyncio.run(again._call(None, GetDialogsRequest())) == "ok"
    other, _ = _wrapped(["ok"], account_id=7002)
    assert asyncio.run(other._call(None, SendMessageRequest())) == "ok"


def test_a_chat_slow_mode_is_not_an_account_pause(fe):
    from telethon.errors import SlowModeWaitError
    client, raw = _wrapped([_pause("SlowModeWaitError", 300), "ok"])
    with pytest.raises(SlowModeWaitError):
        asyncio.run(client._call(None, SendMessageRequest()))
    assert not fe.is_account_cooling(7001)
    assert asyncio.run(client._call(None, SendMessageRequest())) == "ok"


def test_the_floor_never_shortens_a_longer_pause(fe):
    fe.note_floor(7001, "SendMessageRequest", 3600)
    fe.note_floor(7001, "SendMessageRequest", 5)
    assert fe.floor_remaining(7001, "SendMessageRequest") > 3500
    assert fe.seconds_until_ready(7001) > 3500
    assert fe.note_floor(None, "X", 10) == 0.0 and fe.note_floor(7001, "X", 0) == 0.0


def test_every_account_client_gets_the_floor():
    """Сторож: фабрика клиентов вешает порог — иначе гарантия снова зависит
    от каждого вызывающего."""
    import inspect

    from services import account_manager as am
    src = inspect.getsource(am._make_client)
    assert "_wrap_client_flood_floor(_client" in src


def test_the_pause_reaches_the_database_and_is_not_lost_on_a_failed_write(fe, monkeypatch):
    from services import pacing_store
    calls: list[tuple[int, float]] = []
    answers = [False, True]

    async def _apply(pool, account_id, seconds, **kw):
        calls.append((account_id, seconds))
        return answers.pop(0)

    monkeypatch.setattr(fe, "apply_cooldown", _apply)
    fe.note_floor(7001, "SendMessageRequest", 600)
    fe.note_floor(7003, "SendMessageRequest", 5)  # секунды в базу не пишем
    assert asyncio.run(pacing_store.flush_flood_floor(object())) == 0
    # База не приняла — пауза осталась в очереди и дошла со второй попытки.
    assert asyncio.run(pacing_store.flush_flood_floor(object())) == 1
    assert [c[0] for c in calls] == [7001, 7001] and calls[1][1] > 500
    assert asyncio.run(pacing_store.flush_flood_floor(object())) == 0
