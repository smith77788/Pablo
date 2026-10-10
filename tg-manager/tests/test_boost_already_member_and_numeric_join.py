"""Накрутка/вступление: «уже в чате» — успех; числовой id канала резолвится.

Из живого лога операций владельца (гора ошибок):
  • «The authenticated user is already a participant» — аккаунт УЖЕ подписчик,
    но boost_subscribers/community_liven считали это ошибкой (join_channel отдаёт
    already_member с заполненным error). Теперь already_member = успех.
  • «Invalid channel object» — join по ЧИСЛОВОМУ id канала падал: голый
    get_entity на свежей сессии не знает access_hash. Теперь числовой id идёт
    через _resolve_channel_peer (PeerChannel + обход диалогов), как в
    post_to_channel.
"""
from __future__ import annotations

import inspect

from services import op_worker
from services import account_manager


def test_boost_subscribers_counts_already_member_as_success():
    src = inspect.getsource(op_worker._exec_boost_subscribers)
    i = src.index("join_channel(")
    seg = src[i:i + 600]
    # «уже в чате» не должно уходить в провал — проверка error исключает already_member
    assert 'res.get("error") and not res.get("already_member")' in seg, (
        "boost_subscribers снова считает «уже в чате» провалом")


def test_community_liven_counts_already_member():
    src = inspect.getsource(op_worker._exec_community_liven)
    assert 'res.get("already_member")' in src, (
        "community_liven не засчитывает «уже в чате» как члена сообщества")


def test_join_channel_resolves_numeric_id_via_peer_channel():
    src = inspect.getsource(account_manager.join_channel)
    # числовой ref не должен идти голым get_entity — только через _resolve_channel_peer
    assert "_resolve_channel_peer(client, ref_value)" in src, (
        "join_channel не резолвит числовой id канала через _resolve_channel_peer "
        "(вернётся «Invalid channel object»)")
    assert 'ref_value.lstrip("-").isdigit()' in src


def test_bulk_join_still_honors_already_member():
    # Регресс-якорь: bulk_join уже учитывал already_member — не должен потерять.
    src = inspect.getsource(op_worker._exec_bulk_join_inner)
    assert 'not res.get("already_member")' in src
