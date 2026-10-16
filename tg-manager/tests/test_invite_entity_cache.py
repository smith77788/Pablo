"""Резолв сущности группы кэшируется по аккаунту — повторные батчи не дёргают
флуд-лимитированный CheckChatInviteRequest.

Корень FloodWait-шторма из реального лога («A wait of 112s (CheckChatInviteRequest)»):
уже вступивший аккаунт разрешал приватную группу через Import→UserAlreadyParticipant
→ Check НА КАЖДЫЙ батч, и весь флот бил по флуд-лимиту. Теперь сущность кэшируется
по (acc_id, group_ref): повторный резолв берётся из кэша без сети.

Юнит-тест (без Postgres): telethon застаблен в conftest — проверяем механику кэша.
"""
from __future__ import annotations

import asyncio

from services import mass_inviter_engine as inv


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Chat:
    def __init__(self, cid, ah):
        self.id = cid
        self.access_hash = ah


class _PublicClient:
    """Публичная группа резолвится через get_entity — считаем сетевые вызовы."""
    def __init__(self, chat):
        self.chat = chat
        self.calls = 0

    async def get_entity(self, ref):
        self.calls += 1
        return self.chat


class _ExplodingClient:
    """Любое обращение к сети — провал: доказывает, что кэш сработал без сети."""
    async def get_entity(self, ref):
        raise AssertionError("get_entity вызван, хотя должен был сработать кэш")

    async def __call__(self, request):
        raise AssertionError("сетевой запрос вызван, хотя должен был сработать кэш")


def test_public_resolve_is_cached_per_account():
    inv._ENTITY_CACHE.clear()
    try:
        c = _PublicClient(_Chat(555, 111))
        GROUP = "@somegroup"
        # первый резолв аккаунта 1 — сеть; сохранили в кэш
        _run(inv._resolve_group_entity(c, GROUP, acc_id=1))
        assert c.calls == 1
        assert inv._ENTITY_CACHE.get((1, GROUP)) == (555, 111), inv._ENTITY_CACHE
        # повторный резолв того же аккаунта — из кэша, без сети
        _run(inv._resolve_group_entity(c, GROUP, acc_id=1))
        assert c.calls == 1, "повторный резолв обязан идти из кэша"
        # другой аккаунт — свой резолв (access_hash пер-аккаунтный)
        _run(inv._resolve_group_entity(c, GROUP, acc_id=2))
        assert c.calls == 2
        # без acc_id кэш выключен — каждый резолв идёт в сеть
        _run(inv._resolve_group_entity(c, GROUP, acc_id=None))
        _run(inv._resolve_group_entity(c, GROUP, acc_id=None))
        assert c.calls == 4, "без acc_id кэша нет"
    finally:
        inv._ENTITY_CACHE.clear()


def test_private_link_short_circuits_on_cache_hit():
    """Приватная ссылка (+HASH) при попадании в кэш НЕ ходит в сеть — а значит и в
    CheckChatInviteRequest. Пред-populate кэш и резолвим с 'взрывным' клиентом."""
    inv._ENTITY_CACHE.clear()
    try:
        GROUP = "https://t.me/+ABCDEFGHijklmno"
        inv._store_entity(7, GROUP, _Chat(777, 999))
        assert inv._ENTITY_CACHE.get((7, GROUP)) == (777, 999)
        peer = _run(inv._resolve_group_entity(_ExplodingClient(), GROUP, acc_id=7))
        assert peer is not None   # вернулся InputPeerChannel из кэша, сеть не тронута
    finally:
        inv._ENTITY_CACHE.clear()


def test_as_channel_id_parses_numeric_refs():
    assert inv._as_channel_id("2959208816") == 2959208816       # голый id канала
    assert inv._as_channel_id("-1002959208816") == 2959208816   # маркированная форма
    assert inv._as_channel_id(" 2959208816 ") == 2959208816     # с пробелами
    assert inv._as_channel_id("@someuser") is None              # username
    assert inv._as_channel_id("https://t.me/+ABC") is None      # инвайт-ссылка
    assert inv._as_channel_id("-123456") is None                # базовая группа, не канал
    assert inv._as_channel_id("") is None
    assert inv._as_channel_id(None) is None


def test_numeric_channel_id_resolves_via_peerchannel():
    """Приватный канал по голому id резолвится через get_entity(PeerChannel(...)),
    а не по строке (иначе Telegram трактует число как username → «Группа
    недоступна»). Кэшируется, как и остальные."""
    inv._ENTITY_CACHE.clear()
    try:
        c = _PublicClient(_Chat(2959208816, 42))
        _run(inv._resolve_group_entity(c, "2959208816", acc_id=1))
        assert c.calls == 1, "числовой id должен резолвиться (а не падать как username)"
        assert inv._ENTITY_CACHE.get((1, "2959208816")) == (2959208816, 42)
        # повторно — из кэша
        _run(inv._resolve_group_entity(c, "2959208816", acc_id=1))
        assert c.calls == 1
    finally:
        inv._ENTITY_CACHE.clear()


def test_group_channel_id_prefers_id_but_falls_back_to_channel_id():
    """Кэш-хит отдаёт InputPeerChannel (есть .channel_id, НЕТ .id). Без фолбэка
    channel_admin_status вернул бы channel_id=None → «Выдать права инвайтерам»
    ложно рапортовал бы «нет промоутера», хотя аккаунт — админ (жалоба владельца:
    «готовность флота врёт»). Проверяем извлечение id независимо от заглушки
    telethon (у неё любой атрибут — истинный)."""
    class _Entity:            # обычная сущность канала: несёт .id
        id = 555
    class _InputPeer:         # InputPeerChannel из кэша: только .channel_id
        channel_id = 777
    assert inv._group_channel_id(_Entity()) == 555
    assert inv._group_channel_id(_InputPeer()) == 777   # без фикса вернул бы None
    assert inv._group_channel_id(object()) is None


def test_store_ignores_entity_without_access_hash():
    """Сущность без access_hash не кэшируется (нельзя построить InputPeerChannel)."""
    inv._ENTITY_CACHE.clear()
    try:
        class _NoHash:
            id = 5
            access_hash = None
        inv._store_entity(1, "@g", _NoHash())
        assert (1, "@g") not in inv._ENTITY_CACHE
        # без acc_id — тоже не кэшируем
        inv._store_entity(None, "@g", _Chat(1, 2))
        assert not inv._ENTITY_CACHE
    finally:
        inv._ENTITY_CACHE.clear()
