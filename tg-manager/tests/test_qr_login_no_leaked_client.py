"""QR-вход не оставляет подключённых к Telegram клиентов без присмотра.

Два способа потерять живой коннект, оба штатные.

Первый: между `client.connect()` и записью в `_pending_qr` не было никакой
защиты, а на клиента до этой записи НЕТ НИ ОДНОЙ ссылки. Сорвался `qr_login()`
(он отдаёт FloodWait — обычное дело) — и подключённый клиент остаётся висеть
навсегда: отключить его некому, `cleanup_qr_pending` его не найдёт. Оба
вызывающих оборачивают `start_qr_login` во внешний таймаут (мини-апп — в 40 с),
так что прилетает ещё и отмена, а она BaseException и мимо `except Exception`
проходит насквозь.

Второй: пользователь открыл QR и ушёл. Сборщика протухших входов не было, а
`cleanup_qr_pending` зовётся только при НОВОМ входе того же пользователя, —
значит коннект жил до перезапуска процесса.

Почему это дорого именно здесь: лишний живой коннект к Telegram на той же
сессии — прямая дорога к AUTH_KEY_DUPLICATED, самой дорогой поломке продукта.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import account_manager as am


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_pending():
    am._pending_qr.clear()
    yield
    am._pending_qr.clear()


def _client(qr_login_side_effect=None):
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    qr = MagicMock()
    qr.url = "tg://login?token=abc"
    client.qr_login = AsyncMock(
        side_effect=qr_login_side_effect, return_value=qr)
    return client


class _FloodWaitError(Exception):
    def __init__(self, seconds=60):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


def test_flood_on_qr_login_does_not_leak_the_client():
    client = _client(qr_login_side_effect=_FloodWaitError(60))

    with patch.object(am, "_make_client", return_value=client):
        with pytest.raises(_FloodWaitError):
            _run(am.start_qr_login(777))

    client.disconnect.assert_awaited_once(), "подключённый клиент потерян навсегда"
    assert 777 not in am._pending_qr


def test_cancellation_does_not_leak_the_client():
    """Внешний таймаут вызывающего приходит отменой — это BaseException."""
    client = _client(qr_login_side_effect=asyncio.CancelledError())

    with patch.object(am, "_make_client", return_value=client):
        with pytest.raises(asyncio.CancelledError):
            _run(am.start_qr_login(778))

    client.disconnect.assert_awaited_once()
    assert 778 not in am._pending_qr


def test_broken_qr_image_does_not_leave_a_half_started_login():
    """Клиент без пригодного QR в словаре — ожидание кода, которого нет."""
    client = _client()

    with patch.object(am, "_make_client", return_value=client):
        with patch("qrcode.make", side_effect=RuntimeError("нет кодировщика")):
            with pytest.raises(RuntimeError):
                _run(am.start_qr_login(779))

    client.disconnect.assert_awaited_once()
    assert 779 not in am._pending_qr


def test_successful_start_keeps_the_client_for_polling():
    client = _client()

    with patch.object(am, "_make_client", return_value=client):
        png = _run(am.start_qr_login(780))

    assert png, "PNG с кодом не собран"
    assert 780 in am._pending_qr
    client.disconnect.assert_not_awaited()
    # В записи появилось время начала — по нему подбираются заброшенные входы.
    assert len(am._pending_qr[780]) == 4


def test_abandoned_login_is_reaped():
    """Пользователь ушёл — коннект не должен жить до перезапуска процесса."""
    import time

    abandoned = _client()
    qr = MagicMock()
    qr.url = "tg://login?token=old"
    am._pending_qr[999] = (
        abandoned, qr, {}, time.time() - am._QR_PENDING_TTL_S - 1)

    fresh = _client()
    with patch.object(am, "_make_client", return_value=fresh):
        _run(am.start_qr_login(1000))

    abandoned.disconnect.assert_awaited_once()
    assert 999 not in am._pending_qr
    assert 1000 in am._pending_qr


def test_fresh_login_of_another_user_is_not_reaped():
    import time

    recent = _client()
    qr = MagicMock()
    qr.url = "tg://login?token=recent"
    am._pending_qr[1001] = (recent, qr, {}, time.time())

    fresh = _client()
    with patch.object(am, "_make_client", return_value=fresh):
        _run(am.start_qr_login(1002))

    recent.disconnect.assert_not_awaited()
    assert 1001 in am._pending_qr


def test_waiters_still_read_the_entry_correctly():
    """Запись стала длиннее — распаковка в ожидании и в 2FA обязана это пережить."""
    client = _client()
    with patch.object(am, "_make_client", return_value=client):
        _run(am.start_qr_login(1003))

    entry = am._pending_qr[1003]
    stored_client, stored_qr, stored_device = entry[0], entry[1], entry[2]
    assert stored_client is client
    assert stored_qr.url == "tg://login?token=abc"
    assert isinstance(stored_device, dict)
