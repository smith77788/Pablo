"""Экспорт/удаление собственных историй: разбор ответа Telethon (без сети)."""
from __future__ import annotations

from services import story_manager as sm


class _Photo:
    pass


class _Video:
    pass


class _Story:
    def __init__(self, id, caption, media, pinned=False):
        self.id = id
        self.caption = caption
        self.media = media
        self.pinned = pinned
        self.date = None


class _Container:
    def __init__(self, items):
        self.stories = items


class _PeerStories:
    def __init__(self, items):
        self.stories = _Container(items)


def test_parse_stories_extracts_id_caption_mediatype():
    peer = _PeerStories([
        _Story(101, "Привет", _Photo(), pinned=True),
        _Story(102, None, _Video()),
    ])
    got = sm._parse_stories(peer)
    assert [s["id"] for s in got] == [101, 102]
    assert got[0]["caption"] == "Привет"
    assert got[0]["media_type"] == "photo"
    assert got[0]["pinned"] is True
    assert got[1]["caption"] == ""      # None → пустая строка
    assert got[1]["media_type"] == "video"


def test_parse_stories_handles_empty():
    assert sm._parse_stories(_PeerStories([])) == []
    # контейнер как «голый» список StoryItem
    got = sm._parse_stories(_Container([_Story(5, "x", _Photo())]))
    assert [s["id"] for s in got] == [5]
