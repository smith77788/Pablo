"""Фабрики/каналы: channel_service подключён — posts_published в карточке."""
from __future__ import annotations

import asyncio
import os

from services import channel_service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakePool:
    def __init__(self, row=None, count=7):
        self._row = row
        self._count = count

    async def fetchrow(self, q, *a):
        if "managed_channels" in q:
            return self._row
        if "COUNT(*)" in q:
            return {"cnt": self._count}
        return None


def test_get_channel_stats_counts_posts():
    pool = _FakePool(row={"title": "T", "username": "u", "added_at": None}, count=12)
    res = asyncio.run(channel_service.get_channel_stats(pool, 1, 1))
    assert res["posts_published"] == 12
    assert res["title"] == "T" and res["username"] == "u"


def test_channel_detail_returns_posts_published():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    i = src.index("async def channel_detail")
    window = src[i:i + 3000]
    assert "channel_service.get_channel_stats" in window
    assert '"posts_published"' in window


def test_frontend_shows_posts_published():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "d.posts_published" in html
    assert "Публикаций через нас" in html


def test_channel_service_no_longer_marked_orphan():
    src = open(os.path.join(ROOT, "services", "channel_service.py"), encoding="utf-8").read()
    assert "СТАТУС: НЕ ПОДКЛЮЧЁН" not in src
