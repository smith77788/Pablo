"""Резолв канала по числовому id не должен падать «Invalid channel object».

Реальная ошибка из прода (лог операции mass_publish виртуального
администратора):

    target 3945320144: error — "Invalid channel object. Make sure to pass the
    right types..."

Причина: канал резолвился как `client.get_entity(голый_int)`. Для канала на
свежей StringSession (без кэша сущностей) Telethon не знает access_hash и не
понимает, что число — это КАНАЛ: он пробует резолвить его как пользователя и
падает. Из-за этого администратор не мог опубликовать ни одного поста — «тишина
+ ошибка в боте».

Фикс: сначала пробуем `PeerChannel(id)` — это задаёт тип явно, и Telethon сам
добирает access_hash через getChannels (публикующий аккаунт — админ канала, то
есть участник). Голый id остаётся запасным для не-канальных сущностей.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def _tl(monkeypatch):
    """Именованные TL-типы: обычная заглушка conftest отдаёт один и тот же
    объект на любое имя, и отличить PeerChannel от голого int нельзя."""
    cache: dict = {}

    class _TL(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            if name not in cache:
                def _init(self, *a, **kw):
                    if a:
                        self.channel_id = a[0]
                    for k, v in kw.items():
                        setattr(self, k, v)
                cache[name] = type(name, (), {"__init__": _init})
            return cache[name]

    monkeypatch.setitem(sys.modules, "telethon.tl.types", _TL("telethon.tl.types"))

    err = types.ModuleType("telethon.errors")
    err.ChannelPrivateError = type("ChannelPrivateError", (Exception,), {})
    err.ChatAdminRequiredError = type("ChatAdminRequiredError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "telethon.errors", err)

    async def _instant(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)
    return cache


class _Client:
    """get_entity падает «Invalid channel object» на голый int, но резолвит,
    когда ему дают PeerChannel — ровно как настоящий Telethon."""

    def __init__(self):
        self.tried = []
        self.dialogs_scanned = False

    async def get_entity(self, ref):
        self.tried.append(type(ref).__name__)
        if isinstance(ref, int):
            raise TypeError("Invalid channel object. Make sure to pass the right types")
        # PeerChannel → успешный резолв
        return type("Channel", (), {"id": getattr(ref, "channel_id", 0), "access_hash": 777})()

    def iter_dialogs(self, limit=None):
        self.dialogs_scanned = True
        async def _gen():
            if False:
                yield None
        return _gen()


def test_resolve_uses_peerchannel_before_raw_id(_tl, monkeypatch):
    from services import account_manager as am

    c = _Client()
    peer = asyncio.run(am._resolve_channel_peer(c, 3945320144, 0))
    assert peer is not None, "канал не разрезолвлен — повторение прод-бага"
    assert "PeerChannel" in c.tried, "PeerChannel не пробовался"
    assert c.dialogs_scanned is False, "обход диалогов не нужен, если PeerChannel сработал"


def test_access_hash_still_shortcuts(_tl):
    """Когда access_hash известен — прямой InputPeerChannel, без сетевых вызовов."""
    from services import account_manager as am

    c = _Client()
    peer = asyncio.run(am._resolve_channel_peer(c, 3945320144, 555))
    assert getattr(peer, "access_hash", None) == 555
    assert c.tried == [], "при известном access_hash сеть не нужна"


def test_post_to_channel_strategy_uses_peerchannel():
    """Публикация (прямая причина прод-лога) тоже резолвит через PeerChannel."""
    src = (ROOT / "services/account_manager.py").read_text(encoding="utf-8")
    i = src.index("async def post_to_channel")
    body = src[i:i + 4000]
    assert "PeerChannel" in body, "post_to_channel снова резолвит голым id"
    # и именно в стратегии по id, а не только через access_hash
    assert "get_entity(_ref)" in body
