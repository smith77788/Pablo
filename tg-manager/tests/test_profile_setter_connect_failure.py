"""Сеттер профиля: сбой коннекта — это ответ, а не исключение и не утечка.

`client = await _connect(...)` стоял ДО `try` каждой функции. Поэтому занятая
сессия, мёртвый прокси или таймаут уходили вызывающему СЫРЫМ исключением, хотя
все пятнадцать функций файла объявлены возвращающими ``{"ok": ..., "error": ...}``
— мини-апп показывал владельцу «Сервис временно недоступен» вместо причины.

Хуже второе: полуподнятый клиент никто не отключал. Он держал сокет и — через
процессный мьютекс сессии — сам аккаунт до протухания записи, и следующая
подсистема получала «сессия занята» на живом аккаунте.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import profile_setter_engine as pse


def _run(coro):
    return asyncio.run(coro)


class _SessionBusyError(ConnectionError):
    pass


def _failing_client(exc: BaseException):
    client = MagicMock()
    client.connect = AsyncMock(side_effect=exc)
    client.disconnect = AsyncMock()
    return client


def test_connect_failure_disconnects_the_half_built_client():
    """Иначе сокет и мьютекс сессии держат живой аккаунт до протухания."""
    client = _failing_client(_SessionBusyError("сессия занята"))

    with patch("services.account_manager._make_client", return_value=client):
        with pytest.raises(pse.ConnectFailed):
            _run(pse._connect("sess", {"id": 1}))

    client.disconnect.assert_awaited_once()


def test_connect_failure_comes_back_as_a_dict_not_an_exception():
    client = _failing_client(_SessionBusyError("сессия занята"))

    with patch("services.account_manager._make_client", return_value=client):
        res = _run(pse.set_name_bio("sess", {"id": 1}, first_name="Артур"))

    assert res["ok"] is False
    assert "заня" in res["error"].lower(), (
        f"владелец должен прочесть причину, а не трассировку: {res['error']!r}"
    )


def test_timeout_is_explained_in_russian():
    client = MagicMock()

    async def _hang():
        await asyncio.sleep(3600)

    client.connect = AsyncMock(side_effect=_hang)
    client.disconnect = AsyncMock()

    with patch.object(pse, "_CONNECT_TIMEOUT", 0.01):
        with patch("services.account_manager._make_client", return_value=client):
            res = _run(pse.check_restriction("sess", {"id": 1}))

    assert res["ok"] is False
    assert "не ответил" in res["error"]
    client.disconnect.assert_awaited_once()


def test_apply_op_never_raises_on_a_dead_connection():
    """Диспетчер обслуживает и очередь, и мини-апп — он обязан отвечать словарём."""
    client = _failing_client(_SessionBusyError("сессия занята"))

    with patch("services.account_manager._make_client", return_value=client):
        res = _run(pse.apply_op(
            "sess", {"id": 1}, "name", {"name_data": {"first_name": "Артур"}}))

    assert res["ok"] is False and res["error"]


@pytest.mark.parametrize("fn_name,args", [
    ("set_username", ("newname",)),
    ("close_other_sessions", ()),
    ("get_login_code", ()),
    ("set_online", ()),
    ("clear_bio", ()),
])
def test_every_entry_point_survives_a_dead_connection(fn_name, args):
    client = _failing_client(_SessionBusyError("сессия занята"))
    fn = getattr(pse, fn_name)

    with patch("services.account_manager._make_client", return_value=client):
        res = _run(fn("sess", {"id": 1}, *args))

    assert isinstance(res, dict) and res.get("ok") is False
