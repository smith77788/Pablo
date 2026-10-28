"""Server-side channel search stays bounded and scoped to the signed-in owner."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from services import channel_admin as ca


@pytest.mark.asyncio
async def test_page_is_bounded_and_returns_more_flag_and_count():
    pool = AsyncMock()
    now = datetime(2026, 10, 3, tzinfo=UTC)
    rows = [{
        "channel_id": -1000 - i, "title": f"Канал {i}", "username": f"channel{i}",
        "enabled": True, "setup_done": now, "topic": "Тема", "publish_mode": "review",
        "next_post_at": now, "last_post_at": None, "last_error": None,
        "members_count": 12, "pending_drafts": 2, "_total_count": 61,
    } for i in range(31)]
    pool.fetch.return_value = rows

    result = await ca.list_channels_page(pool, 42, page=1, page_size=30, query="Канал", state="attention")

    sql, owner, query, state, limit, offset = pool.fetch.call_args.args
    assert owner == 42 and query == "Канал" and state == "attention"
    assert limit == 31 and offset == 30
    assert "mc.owner_id=$1" in sql and "d.owner_id=mc.owner_id" in sql
    assert "count(*) OVER()" in sql and "strpos(lower" in sql
    assert len(result["channels"]) == 30 and result["has_more"] is True
    assert result["total"] == 61 and result["page"] == 1
    assert result["channels"][0]["channel_id"] == "-1000"
    assert result["channels"][0]["next_post_at"] == now.isoformat()
    assert result["channels"][0]["pending_drafts"] == 2
    assert "_total_count" not in result["channels"][0]


@pytest.mark.asyncio
async def test_last_page_is_bounded_and_has_no_next_page():
    pool = AsyncMock()
    pool.fetch.return_value = [{"channel_id": 7, "setup_done": None, "_total_count": 31}]
    result = await ca.list_channels_page(pool, 42, page=1)
    assert len(result["channels"]) == 1
    assert result["channels"][0]["installed"] is False
    assert result["has_more"] is False
    assert pool.fetch.call_args.args[-2:] == (31, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"page": -1}, {"page": True}, {"page": 100_001}, {"page_size": 0},
    {"page_size": 101}, {"query": "x" * 151}, {"query": []}, {"state": "unknown"},
    {"state": []},
])
async def test_invalid_page_search_or_filter_is_rejected(kwargs):
    pool = AsyncMock()
    with pytest.raises(ca.ChannelAdminError):
        await ca.list_channels_page(pool, 42, **kwargs)
    pool.fetch.assert_not_awaited()
