"""Story Manager (раздел 12/3 паритета TE) — публикация своей истории.

Чистые хелперы (тип медиа, период) + пути post_story на мок-Telethon:
успех, «нельзя публиковать» (нет Premium), ошибка скачивания, нет сессии.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import story_manager as sm


def _run(coro):
    return asyncio.run(coro)


def test_normalize_period():
    assert sm.normalize_period(6) == 6 * 3600
    assert sm.normalize_period(24) == 24 * 3600
    assert sm.normalize_period(48) == 48 * 3600
    assert sm.normalize_period(99) == 24 * 3600      # недопустимое → 24ч
    assert sm.normalize_period("bad") == 24 * 3600


def test_classify_media():
    assert sm.classify_media("video/mp4", "http://x/a") == "video"
    assert sm.classify_media("", "http://x/clip.MP4?y=1") == "video"
    assert sm.classify_media("image/jpeg", "http://x/a.jpg") == "photo"
    assert sm.classify_media("", "http://x/pic.png") == "photo"


def test_no_session():
    assert _run(sm.post_story("x", "http://x/a.jpg"))["status"] == "error"


def test_no_media_url():
    res = _run(sm.post_story("session-string-long-enough", "  "))
    assert res["status"] == "error" and "медиа" in res["error"].lower()


def _fake_client(can_value, authorized=True):
    # telethon застаблен в тестах → тип запроса не различить по имени. Полагаемся
    # на ПОРЯДОК: 1-й client(...) = CanSendStoryRequest, 2-й = SendStoryRequest.
    client = AsyncMock(side_effect=[can_value, object(), object()])
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=authorized)
    client.upload_file = AsyncMock(return_value=object())
    return client


def test_posts_story_success():
    class _Can:
        count = 3
    client = _fake_client(_Can())
    with patch.object(sm, "_download", AsyncMock(return_value=(b"jpegbytes", "image/jpeg"))), \
         patch("services.account_manager._make_client", return_value=client):
        res = _run(sm.post_story("session-string-long-enough", "http://x/a.jpg", "hi", 24, {"id": 1}))
    assert res["ok"] is True and res["status"] == "posted" and res["media_type"] == "photo"


def test_cant_post_when_no_premium():
    class _Can:
        count = 0
    client = _fake_client(_Can())
    with patch.object(sm, "_download", AsyncMock(return_value=(b"jpegbytes", "image/jpeg"))), \
         patch("services.account_manager._make_client", return_value=client):
        res = _run(sm.post_story("session-string-long-enough", "http://x/a.jpg", "", 24, {}))
    assert res["ok"] is False and res["status"] == "cant_post"


def test_download_error_surfaced():
    with patch.object(sm, "_download", AsyncMock(side_effect=ValueError("HTTP 404"))):
        res = _run(sm.post_story("session-string-long-enough", "http://x/missing.jpg"))
    assert res["ok"] is False and res["status"] == "error" and "404" in res["error"]


def test_route_and_engine_wired():
    import inspect
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    assert 'app.router.add_post("/api/miniapp/account/{acc_id}/post_story", account_post_story)' in src
    assert hasattr(sm, "post_story")
