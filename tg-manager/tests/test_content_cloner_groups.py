"""Chat Cloner (раздел 12 паритета TE) — покрыт ЕДИНЫМ Content Cloner.

Отдельный «Chat Cloner» был бы дублем (правило: одна стабильная версия модулей,
без v1/v2). Движок content_cloner_engine целе-агностичен — get_entity +
forward_messages работают и с группами. Эти тесты фиксируют, что клонирование
в ГРУППУ реально исполняется, а parse_channel_ref принимает групповые ссылки.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services import content_cloner_engine as cce


def _run(coro):
    return asyncio.run(coro)


def test_parse_channel_ref_accepts_group_refs():
    # публичная группа по @username и приватная invite-ссылка группы
    assert cce.parse_channel_ref("@my_group_chat") == "@my_group_chat"
    assert cce.parse_channel_ref("-1001234567890") == "-1001234567890"
    # приватный invite (общий формат для групп и каналов) не должен коверкаться
    got = cce.parse_channel_ref("https://t.me/+AbCdEf12345")
    assert "+AbCdEf12345" in got or got == "https://t.me/+AbCdEf12345"


def test_clone_forwards_into_group_target():
    """clone_to_channel реально пересылает сообщения в ЦЕЛЬ-ГРУППУ."""
    group_entity = MagicMock(name="megagroup", megagroup=True, broadcast=False, id=777)
    source_entity = MagicMock(name="source", id=111)

    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    # источник и цель по порядку вызовов get_entity
    client.get_entity = AsyncMock(side_effect=[source_entity, group_entity])
    client.forward_messages = AsyncMock(return_value=None)
    client.disconnect = AsyncMock()

    with patch.object(cce, "_make_client", return_value=client):
        res = _run(cce.clone_to_channel(
            "enc-session", {"proxy_url": ""},
            source_ref="@src_channel", target_ref="@dst_group",
            msg_ids=[1, 2, 3], mode="forward",
        ))

    assert res["ok"] == 3 and res["fail"] == 0 and not res["errors"]
    # цель — именно group_entity
    args, kwargs = client.forward_messages.call_args
    assert args[0] is group_entity
    assert list(args[1]) == [1, 2, 3]
    assert args[2] is source_entity


def test_clone_reports_no_access_to_group_gracefully():
    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    src = MagicMock(id=1)
    client.get_entity = AsyncMock(side_effect=[src, PermissionError("CHANNEL_PRIVATE")])
    client.disconnect = AsyncMock()

    with patch.object(cce, "_make_client", return_value=client):
        res = _run(cce.clone_to_channel(
            "s", {"proxy_url": ""},
            source_ref="@src", target_ref="@private_group",
            msg_ids=[1, 2], mode="forward",
        ))
    assert res["ok"] == 0 and res["fail"] == 2 and res["errors"]
