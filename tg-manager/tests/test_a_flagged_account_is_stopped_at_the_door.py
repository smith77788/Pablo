"""Ограниченный аккаунт и суточный лимит действуют на любом пути к Telegram.

ЧТО БЫЛО. Оба правила жили в подборе аккаунтов очереди операций. Действие,
начатое мимо очереди (кнопка бота, мини-приложение, фоновый цикл, аккаунт,
выбранный вручную), их не проходило: аккаунт под спам-блоком продолжал
приглашать и слать жалобы, а вступления и реакции мимо очереди в суточный
лимит не попадали — очередь считала аккаунт свежим и добирала полный лимит
поверх уже сделанного.

ЧТО ТЕПЕРЬ. Клиент аккаунта сам отказывает в том, что ограничение запрещает,
и сам считает действия, которые не посчитал исполнитель операции.
"""
from __future__ import annotations

import asyncio

import pytest


class InviteToChannelRequest:
    pass


class JoinChannelRequest:
    pass


class GetDialogsRequest:
    pass


class InputPeerUser:
    def __init__(self, user_id):
        self.user_id = user_id


class InputPeerChannel:
    channel_id = 5


class SendMessageRequest:
    def __init__(self, peer):
        self.peer = peer


class _Client:
    def __init__(self):
        self.sent: list[str] = []

    async def _call(self, sender, request, ordered=False):
        self.sent.append(type(request).__name__)
        return "ok"


@pytest.fixture
def gate():
    from services import account_gate
    saved = dict(account_gate._STATUS), dict(account_gate._direct_unsaved)
    account_gate._STATUS.clear()
    account_gate._direct_unsaved.clear()
    yield account_gate
    for live, old in zip((account_gate._STATUS, account_gate._direct_unsaved), saved):
        live.clear()
        live.update(old)


def _wrapped(account_id=7101):
    from services import account_manager as am
    raw = _Client()
    return am._wrap_client_flood_floor(raw, account_id), raw


def test_a_spam_blocked_account_does_not_invite_from_any_path(gate):
    gate.set_status(7101, "spamblock")
    client, raw = _wrapped()
    with pytest.raises(gate.AccountRestrictedError) as e:
        asyncio.run(client._call(None, InviteToChannelRequest()))
    assert raw.sent == [], "запрос не должен был дойти до Telegram"
    assert "@SpamBot" in str(e.value) and "ограничени" in str(e.value)


def test_what_telegram_still_allows_a_spam_blocked_account_keeps_working(gate):
    gate.set_status(7101, "spamblock")
    client, raw = _wrapped()
    for req in (GetDialogsRequest(), JoinChannelRequest(),
                SendMessageRequest(InputPeerUser(55)),          # ответ написавшему
                SendMessageRequest(InputPeerUser(gate.SPAMBOT_USER_ID))):
        assert asyncio.run(client._call(None, req)) == "ok"
    assert len(raw.sent) == 4


def test_a_frozen_account_only_reads_and_appeals(gate):
    gate.set_status(7101, "frozen")
    client, raw = _wrapped()
    for req in (JoinChannelRequest(), SendMessageRequest(InputPeerUser(55))):
        with pytest.raises(gate.AccountRestrictedError):
            asyncio.run(client._call(None, req))
    for req in (GetDialogsRequest(), SendMessageRequest(InputPeerChannel()),
                SendMessageRequest(InputPeerUser(gate.SPAMBOT_USER_ID))):
        assert asyncio.run(client._call(None, req)) == "ok"
    assert raw.sent == ["GetDialogsRequest", "SendMessageRequest", "SendMessageRequest"]


def test_a_healthy_account_and_a_lifted_block_are_not_refused(gate):
    client, raw = _wrapped()
    assert asyncio.run(client._call(None, InviteToChannelRequest())) == "ok"
    gate.set_status(7101, "spamblock")
    gate.set_status(7101, "active")        # блок сняли
    assert asyncio.run(client._call(None, InviteToChannelRequest())) == "ok"


def test_peer_flood_closes_the_door_at_once(gate):
    from services import flood_engine
    saved = dict(flood_engine._flood_state)
    try:
        asyncio.run(flood_engine.record_peer_flood(None, 7101, "invite"))
    finally:
        flood_engine._flood_state.clear()
        flood_engine._flood_state.update(saved)
    client, raw = _wrapped()
    with pytest.raises(gate.AccountRestrictedError):
        asyncio.run(client._call(None, InviteToChannelRequest()))


def test_a_join_outside_the_queue_is_counted_and_inside_is_not_counted_twice(gate):
    from services import logger
    client, raw = _wrapped()

    async def _direct():
        await client._call(None, JoinChannelRequest())
        await client._call(None, JoinChannelRequest())
        await client._call(None, GetDialogsRequest())      # чтение не считается

    async def _in_operation():
        logger.set_correlation_id(op_id="op42")
        await client._call(None, JoinChannelRequest())

    asyncio.run(_direct())
    asyncio.run(_in_operation())       # это действие запишет исполнитель
    assert gate.drain_direct_actions() == [(7101, "join", 2)]
    assert gate.drain_direct_actions() == []


def test_a_refused_or_failed_request_is_not_counted(gate):
    client, raw = _wrapped()

    async def _boom(sender, request, ordered=False):
        raise RuntimeError("сеть")

    raw2 = _Client()
    raw2._call = _boom
    from services import account_manager as am
    failing = am._wrap_client_flood_floor(raw2, 7102)
    with pytest.raises(RuntimeError):
        asyncio.run(failing._call(None, JoinChannelRequest()))
    assert gate.drain_direct_actions() == []


def test_a_spam_block_nobody_recorded_still_stops_the_account(gate):
    """PEER_FLOOD ловят 17 файлов, а записывают четыре. Экран показал ошибку —
    и аккаунт уходил в следующие приглашения прямо под блок."""
    import telethon.errors as errors
    from services import account_manager as am, flood_engine
    saved = dict(flood_engine._flood_state)
    saved_q = dict(gate._peer_flood_unsaved)
    gate._peer_flood_unsaved.clear()

    class _Blocked(_Client):
        async def _call(self, sender, request, ordered=False):
            self.sent.append(type(request).__name__)
            raise errors.PeerFloodError("PEER_FLOOD")

    raw = _Blocked()
    client = am._wrap_client_flood_floor(raw, 7103)
    try:
        with pytest.raises(errors.PeerFloodError):
            asyncio.run(client._call(None, InviteToChannelRequest()))
        # Вызывающий ничего не записал, а аккаунт уже остановлен везде.
        with pytest.raises(gate.AccountRestrictedError):
            asyncio.run(client._call(None, InviteToChannelRequest()))
        assert raw.sent == ["InviteToChannelRequest"], "второй запрос ушёл под блок"
        assert flood_engine.is_account_cooling(7103)
        assert flood_engine.seconds_until_ready(7103) > 47 * 3600
        assert gate.drain_peer_floods() == [(7103, "InviteToChannelRequest")]
    finally:
        flood_engine._flood_state.clear()
        flood_engine._flood_state.update(saved)
        gate._peer_flood_unsaved.clear()
        gate._peer_flood_unsaved.update(saved_q)


def test_a_ban_seen_outside_the_queue_takes_the_account_out_of_rotation(gate):
    """Экран или фоновый цикл получил «аккаунт заблокирован» и ничего не
    записал: подбор продолжал отдавать его в операции, и каждая падала."""
    import telethon.errors as errors
    from services import account_manager as am, flood_engine
    saved = dict(flood_engine._flood_state)
    saved_q = dict(gate._ban_unsaved)
    gate._ban_unsaved.clear()

    class _Banned(_Client):
        async def _call(self, sender, request, ordered=False):
            raise errors.UserDeactivatedBanError("USER_DEACTIVATED_BAN")

    client = am._wrap_client_flood_floor(_Banned(), 7104)
    try:
        with pytest.raises(errors.UserDeactivatedBanError):
            asyncio.run(client._call(None, GetDialogsRequest()))
        assert flood_engine.is_account_cooling(7104)
        assert gate.drain_bans() == [(7104, "UserDeactivatedBanError")]
    finally:
        flood_engine._flood_state.clear()
        flood_engine._flood_state.update(saved)
        gate._ban_unsaved.clear()
        gate._ban_unsaved.update(saved_q)


def test_a_revoked_key_is_not_a_ban_at_the_door(gate):
    """AUTH_KEY_UNREGISTERED даёт и наш собственный сбой транспорта. На общем
    входе контекста нет — живой аккаунт выключать по нему нельзя."""
    import telethon.errors as errors
    from services import account_manager as am
    saved_q = dict(gate._ban_unsaved)
    gate._ban_unsaved.clear()

    class _NoKey(_Client):
        async def _call(self, sender, request, ordered=False):
            raise errors.AuthKeyUnregisteredError("AUTH_KEY_UNREGISTERED")

    client = am._wrap_client_flood_floor(_NoKey(), 7105)
    try:
        with pytest.raises(errors.AuthKeyUnregisteredError):
            asyncio.run(client._call(None, GetDialogsRequest()))
        assert gate.drain_bans() == []
    finally:
        gate._ban_unsaved.clear()
        gate._ban_unsaved.update(saved_q)
