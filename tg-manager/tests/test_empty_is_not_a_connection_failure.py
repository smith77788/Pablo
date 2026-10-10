"""Регресс: сбой подключения не должен выглядеть как «каналов нет».

`get_dialogs` глушила три разных сбоя в ОДИН пустой список: таймаут коннекта,
сетевую ошибку (в том числе занятую сессию) и любое прочее исключение. Экран
бота после этого показывал владельцу «📭 Каналов и групп не найдено» — то есть
мёртвый прокси, обрыв сети и занятый аккаунт объяснялись владельцу как пустой
аккаунт. Он идёт проверять не то, а настоящая причина не видна нигде, кроме
лога.

Пустой список при УСПЕШНОМ обходе диалогов — по-прежнему пустой список: «нет
каналов» и «не смогли спросить» должны различаться, а не меняться местами.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from services import account_manager as am


ACC = {"id": 3, "proxy_url": "socks5://10.0.0.1:1080"}


class _FailingClient:
    def __init__(self, exc):
        self._exc = exc
        self.disconnected = False

    async def connect(self):
        raise self._exc

    async def disconnect(self):
        self.disconnected = True

    def iter_dialogs(self, *a, **k):
        raise AssertionError("диалоги не запрашиваются без коннекта")


class _EmptyClient:
    def __init__(self):
        self.disconnected = False

    async def connect(self):
        return None

    def iter_dialogs(self, *a, **k):
        class _It:
            async def __anext__(self):
                raise StopAsyncIteration

        return _It()

    async def disconnect(self):
        self.disconnected = True


def _dialogs(monkeypatch, client, **kw):
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: client)
    return asyncio.run(am.get_dialogs("session", _acc=dict(ACC), **kw))


@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError(),
    ConnectionRefusedError("connection refused"),
    OSError("[Errno -3] Temporary failure in name resolution"),
    am.SessionBusyError("сессия занята другим подключением"),
    RuntimeError("что-то пошло не так"),
])
def test_connection_failure_is_not_an_empty_account(monkeypatch, exc):
    with pytest.raises(Exception) as got:
        _dialogs(monkeypatch, _FailingClient(exc), raise_on_failure=True)
    assert not isinstance(got.value, StopAsyncIteration)


@pytest.mark.parametrize("exc,expected", [
    (asyncio.TimeoutError(), "время ожидания"),
    (ConnectionRefusedError("refused"), "сет"),
    (am.SessionBusyError("занята"), "занят"),
])
def test_the_reason_reaches_the_owner_in_russian(monkeypatch, exc, expected):
    with pytest.raises(am.DialogsUnavailableError) as got:
        _dialogs(monkeypatch, _FailingClient(exc), raise_on_failure=True)
    text = str(got.value)
    assert expected in text.lower(), text
    # «Telegram» — имя собственное и по-русски пишется так же; остальная
    # латиница в тексте владельцу означает, что сюда уехал сырой английский.
    assert not re.search(r"[A-Za-z]{4,}", text.replace("Telegram", "")), (
        f"владельцу уехал английский: {text}")


def test_busy_session_is_told_apart_from_a_dead_proxy(monkeypatch):
    """Занятая сессия и мёртвый прокси требуют РАЗНЫХ действий владельца."""
    with pytest.raises(am.DialogsUnavailableError) as busy:
        _dialogs(monkeypatch, _FailingClient(am.SessionBusyError("занята")),
                 raise_on_failure=True)
    with pytest.raises(am.DialogsUnavailableError) as net:
        _dialogs(monkeypatch, _FailingClient(ConnectionRefusedError("refused")),
                 raise_on_failure=True)
    assert str(busy.value) != str(net.value)
    assert "прокси" not in str(busy.value).lower(), (
        "занятую сессию нельзя объяснять прокси — владелец пойдёт чинить исправное"
    )


def test_missing_session_is_named_as_such(monkeypatch):
    with pytest.raises(am.DialogsUnavailableError) as got:
        asyncio.run(am.get_dialogs("", _acc=dict(ACC), raise_on_failure=True))
    assert "сесси" in str(got.value).lower()


def test_an_account_without_channels_still_returns_empty(monkeypatch):
    """«Каналов нет» — законный ответ, и он не должен стать ошибкой."""
    client = _EmptyClient()
    assert _dialogs(monkeypatch, client, raise_on_failure=True) == []
    assert client.disconnected is True


def test_default_behaviour_is_unchanged_for_bulk_callers(monkeypatch):
    """Массовые исполнители на пустом списке просто пропускают аккаунт —
    их поведение менять нельзя, это чужая зона."""
    for exc in (asyncio.TimeoutError(), ConnectionRefusedError("refused"),
                RuntimeError("boom")):
        assert _dialogs(monkeypatch, _FailingClient(exc)) == []


def test_client_is_closed_even_when_the_reason_is_raised(monkeypatch):
    client = _FailingClient(ConnectionRefusedError("refused"))
    with pytest.raises(am.DialogsUnavailableError):
        _dialogs(monkeypatch, client, raise_on_failure=True)
    assert client.disconnected is True, "клиент оставлен открытым"


def test_dead_session_still_reaches_the_caller(monkeypatch):
    """Мёртвую сессию по-прежнему поднимаем как есть: по ней вызывающий
    ставит аккаунту статус session_expired."""
    from telethon.errors import AuthKeyUnregisteredError

    exc = AuthKeyUnregisteredError(None)
    with pytest.raises(AuthKeyUnregisteredError):
        _dialogs(monkeypatch, _FailingClient(exc), raise_on_failure=True)
    with pytest.raises(AuthKeyUnregisteredError):
        _dialogs(monkeypatch, _FailingClient(exc))


def test_owner_facing_screens_ask_for_the_reason():
    """Храповик: экраны бота, показывающие список владельцу, обязаны просить
    причину. Без этого они снова начнут врать «каналов не найдено»."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "bot" / "handlers" / "accounts.py").read_text(encoding="utf-8")
    calls = re.findall(r"await get_dialogs\((?:[^()]|\([^()]*\))*\)", src)
    assert len(calls) == 3, f"изменилось число экранов со списком: {len(calls)}"
    for c in calls:
        assert "raise_on_failure=True" in c, c
    assert src.count("except DialogsUnavailableError as exc:") == 3

