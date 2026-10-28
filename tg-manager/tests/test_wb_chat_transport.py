"""Транспортный слой WB Chat: мок-драйвер, фабрика, реальная заглушка.

Чистые тесты без БД и сети — проверяют контракт, на который опирается весь
аккаунтный слой.
"""
from __future__ import annotations

import pytest

from services.wb_chat import transport as T
from services.wb_chat.drivers.mock import MockWBChatTransport, SENT_OUTBOX, reset_mock_state
from services.wb_chat.drivers.real import RealWBChatTransport


@pytest.fixture(autouse=True)
def _clean():
    reset_mock_state()
    yield
    reset_mock_state()


# ── Фабрика драйверов ────────────────────────────────────────────────────────
def test_build_transport_mock_and_real():
    assert isinstance(T.build_transport(driver="mock"), MockWBChatTransport)
    assert isinstance(T.build_transport(driver="real"), RealWBChatTransport)


def test_build_transport_unknown_driver():
    with pytest.raises(T.WBChatError):
        T.build_transport(driver="nope")


# ── Мок: вход по номеру ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mock_login_flow():
    tr = MockWBChatTransport()
    async with tr:
        assert await tr.is_authorized() is False
        ch = await tr.start_login("+79990001122")
        session = await tr.complete_login(ch, "0000")
        assert session.user is not None
        assert session.user.phone == "+79990001122"
        assert await tr.is_authorized() is True


@pytest.mark.asyncio
async def test_mock_login_wrong_code():
    tr = MockWBChatTransport()
    await tr.connect()
    ch = await tr.start_login("+79990001122")
    with pytest.raises(T.WBAuthError):
        await tr.complete_login(ch, "9999")


@pytest.mark.asyncio
async def test_mock_session_roundtrip():
    tr = MockWBChatTransport()
    ch = await tr.start_login("+70001112233")
    session = await tr.complete_login(ch, "0000")
    # Поднимаем новый транспорт из экспортированной сессии — авторизован без входа.
    tr2 = MockWBChatTransport(session=session.data)
    assert await tr2.is_authorized() is True
    me = await tr2.get_me()
    assert me.phone == "+70001112233"


# ── Мок: операции и ошибки ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mock_send_message_records_outbox():
    tr = MockWBChatTransport()
    ch = await tr.start_login("+71112223344")
    await tr.complete_login(ch, "0000")
    msg = await tr.send_message("@channel", "привет")
    assert msg.text == "привет"
    assert SENT_OUTBOX["channel"] == ["привет"]


@pytest.mark.asyncio
async def test_mock_send_requires_auth():
    tr = MockWBChatTransport()
    with pytest.raises(T.WBAuthError):
        await tr.send_message("@x", "hi")


@pytest.mark.asyncio
async def test_mock_invalid_and_flood_markers():
    tr = MockWBChatTransport()
    ch = await tr.start_login("+70000000000")
    await tr.complete_login(ch, "0000")
    with pytest.raises(T.WBPeerInvalid):
        await tr.send_message("invalid:someone", "hi")
    with pytest.raises(T.WBFloodWait):
        await tr.send_message("flood:target", "hi")


# ── Реальная заглушка ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_real_driver_raises_protocol_unavailable():
    tr = RealWBChatTransport()
    with pytest.raises(T.WBProtocolUnavailable):
        await tr.connect()
    with pytest.raises(T.WBProtocolUnavailable):
        await tr.send_message("@x", "hi")


def test_flood_wait_carries_seconds():
    err = T.WBFloodWait(42)
    assert err.seconds == 42
