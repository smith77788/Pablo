"""Фото проходят путь владелец -> медиатека -> черновик -> очередь без подмены."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from services import channel_admin as ca
from services import (
    geo_va_link,
    mini_app_va_media,
    operation_bus,
    va_control,
    va_media,
    va_strategy,
)


@pytest.fixture
def pool():
    return SimpleNamespace(fetchrow=AsyncMock(), fetch=AsyncMock(return_value=[]), execute=AsyncMock())


async def test_add_checks_ownership_before_writing(pool, monkeypatch):
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value=None))
    with pytest.raises(va_media.MediaError, match="Канал не найден"):
        await va_media.add_photo(pool, 5, 9, "file", "unique", "Фото конференции")
    pool.fetchrow.assert_not_awaited()


async def test_add_scopes_photo_and_preserves_repeat_cooldown(pool, monkeypatch):
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Канал"}))
    pool.fetchrow.return_value = {"id": 12, "description": "Фото конференции"}
    assert (await va_media.add_photo(pool, 5, 9, "file", "unique", "Фото конференции"))["id"] == 12
    sql, *args = pool.fetchrow.call_args.args
    assert args == [5, 9, "file", "unique", "Фото конференции"]
    assert "ON CONFLICT(owner_id,channel_id,file_unique_id)" in sql
    assert "selected_at" not in sql


async def test_matching_and_atomic_repeat_protection(pool):
    pool.fetch.return_value = [{"id": 1, "description": "Деловая конференция, встреча гостей"},
                               {"id": 2, "description": "Сопровождение гостей конференции"},
                               {"id": 3, "description": "Букет свежих цветов"}]
    photo = {"id": 2, "file_id": "photo", "description": "Сопровождение гостей конференции"}
    pool.fetchrow.side_effect = [None, photo]
    chosen = await va_media.choose_photo(pool, 5, 9, "Деловая конференция: сопровождение гостей.")
    assert chosen == photo
    sql, *scope = pool.fetch.call_args.args
    assert scope == [5, 9]
    assert "owner_id=$1 AND channel_id=$2" in sql
    assert "interval '7 days'" in sql and "LIMIT 100" in sql
    for call in pool.fetchrow.call_args_list:
        sql, _, owner, channel = call.args
        assert (owner, channel) == (5, 9)
        assert "owner_id=$2 AND channel_id=$3" in sql
        assert "selected_at < now() - interval '7 days'" in sql


@pytest.mark.parametrize("text", ["Ваза с цветами", "Коротко", "я" * 901, "😀" * 451])
async def test_unrelated_or_long_text_does_not_get_arbitrary_photo(pool, text):
    pool.fetch.return_value = [{"id": 1, "description": "Деловая конференция"}]
    assert await va_media.choose_photo(pool, 5, 9, text) is None
    pool.fetchrow.assert_not_awaited()


async def test_disabled_or_foreign_photo_fails_closed(pool):
    pool.fetchrow.return_value = None
    with pytest.raises(va_media.MediaError):
        await va_media.get_photo(pool, 5, 9, 12)
    sql, *scope = pool.fetchrow.call_args.args
    assert scope == [12, 5, 9]
    assert "owner_id=$2" in sql and "channel_id=$3" in sql and "AND enabled" in sql
    assert "managed_channels" in sql


async def test_draft_persists_selected_photo(pool, monkeypatch):
    monkeypatch.setattr(va_media, "choose_photo", AsyncMock(return_value={"id": 12}))
    pool.fetchrow.return_value = {"id": 33}
    draft = ca.Draft("Конференции", "Текст", [], False, True)
    assert await ca.save_draft(pool, 5, 9, draft) == 33
    sql, *values = pool.fetchrow.call_args.args
    assert "media_id" in sql and values[-1] == 12


@pytest.mark.parametrize("media_id", [None, 12])
async def test_approval_never_reselects_another_photo(pool, monkeypatch, media_id):
    monkeypatch.setattr(ca, "_claim_draft", AsyncMock(return_value={
        "id": 33, "channel_id": 9, "pillar": "Рубрика", "body": "Текст",
        "is_intro": False, "media_id": media_id,
    }))
    publish = AsyncMock(return_value=44)
    monkeypatch.setattr(ca, "publish", publish)
    monkeypatch.setattr(ca, "_after_publish", AsyncMock())
    monkeypatch.setattr(ca, "log_event", AsyncMock())
    await ca.publish_draft(pool, 5, 33)
    publish.assert_awaited_once_with(pool, 5, 9, "Текст", "Рубрика", media_id=media_id, select_media=False)


async def test_photo_reaches_operation_bus(pool, monkeypatch):
    monkeypatch.setattr(va_control, "eligible_accounts", AsyncMock(return_value=[8]))
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Канал"}))
    monkeypatch.setattr(va_media, "get_photo", AsyncMock(return_value={"file_id": "photo-id"}))
    submit = AsyncMock(return_value=44)
    monkeypatch.setattr(operation_bus, "submit", submit)
    assert await ca.publish(pool, 5, 9, "Текст", "Рубрика", media_id=12, select_media=False) == 44
    params = submit.call_args.args[3]
    assert params["media_file_id"] == "photo-id" and params["media_type"] == "photo"
    assert params["require_media"] is True and params["channel_ids"] == [9]


async def test_revoked_photo_cannot_enqueue(pool, monkeypatch):
    monkeypatch.setattr(va_control, "eligible_accounts", AsyncMock(return_value=[8]))
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Канал"}))
    monkeypatch.setattr(va_media, "get_photo", AsyncMock(side_effect=va_media.MediaError("Фото отключено")))
    submit = AsyncMock()
    monkeypatch.setattr(operation_bus, "submit", submit)
    with pytest.raises(ca.ChannelAdminError, match="Фото отключено"):
        await ca.publish(pool, 5, 9, "Текст", "Рубрика", media_id=12, select_media=False)
    submit.assert_not_awaited()


def test_missing_required_download_is_not_a_text_only_success():
    assert va_media.require_download({"require_media": True}, None)["status"] == "failed"
    assert va_media.require_download({"require_media": True}, b"")["status"] == "failed"
    assert va_media.require_download({"require_media": True}, b"photo") is None
    assert va_media.require_download({}, None) is None
    source = (Path(__file__).resolve().parents[1] / "services/op_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    worker = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "_exec_mass_publish")
    text = ast.get_source_segment(source, worker)
    assert text.index("require_download(params, media_bytes)") < text.index("media_bytes=media_bytes")
    assert "if media_failure:\n        return media_failure" in text


async def test_bot_upload_is_explicit_approval(pool, monkeypatch):
    from bot.handlers.va_admin import va_add_photo
    message = SimpleNamespace(caption="/va_photo 9 Деловая конференция", text=None,
                              from_user=SimpleNamespace(id=5), answer=AsyncMock(),
                              photo=[SimpleNamespace(file_id="file", file_unique_id="unique", file_size=512)])
    add = AsyncMock(return_value={"id": 12})
    monkeypatch.setattr(va_media, "add_photo", add)
    await va_add_photo(message, pool)
    add.assert_awaited_once_with(pool, 5, 9, "file", "unique", "Деловая конференция")


async def test_preview_and_delete_are_owner_scoped(pool, monkeypatch):
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Канал"}))
    get = AsyncMock(return_value={"file_id": "private"})
    download = AsyncMock(return_value="data:image/jpeg;base64,/9j/")
    remove = AsyncMock()
    monkeypatch.setattr(va_media, "get_photo", get)
    monkeypatch.setattr(va_media, "remove_photo", remove)
    monkeypatch.setattr(mini_app_va_media, "download_photo", download)
    app = web.Application()
    mini_app_va_media.setup_routes(app, pool, lambda request: 5)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/miniapp/va/channel/9/media/12")
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
        assert "private" not in await response.text()
        get.assert_awaited_once_with(pool, 5, 9, 12)
        assert (await client.delete("/api/miniapp/va/channel/9/media/12")).status == 200
        remove.assert_awaited_once_with(pool, 5, 9, 12)


@pytest.mark.parametrize("owner", [None, 5])
async def test_unauthorized_preview_does_not_read_or_download(pool, monkeypatch, owner):
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value=None))
    get, download = AsyncMock(), AsyncMock()
    monkeypatch.setattr(va_media, "get_photo", get)
    monkeypatch.setattr(mini_app_va_media, "download_photo", download)
    app = web.Application()
    mini_app_va_media.setup_routes(app, pool, lambda request: owner)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/miniapp/va/channel/9/media/12")
        assert response.status == (401 if owner is None else 404)
        get.assert_not_awaited()
        download.assert_not_awaited()


def test_preview_buffer_is_bounded(monkeypatch):
    monkeypatch.setattr(va_media, "MAX_PHOTO_BYTES", 10)
    buffer = mini_app_va_media._PhotoBuffer()
    buffer.write(b"12345")
    with pytest.raises(va_media.MediaError):
        buffer.write(b"123456")


def test_local_geography_and_service_limits_reach_both_prompts():
    own, errors = ca.validate_business({"geography": "Киев", "service_limits": "Только сопровождение мероприятий"})
    assert not errors
    profile = va_strategy.apply_strategy({"business": own}, va_strategy.validate({
        "enabled": True, "destination": "@manager", "action": "Записаться", "business": {
            "geography": "Другой город", "service_limits": "По предварительной записи"},
    }))
    assert profile["business"]["geography"] == "Киев"
    for _, prompt in (ca.build_post_prompt(profile, pillar="Мероприятия"),
                      ca.build_plan_prompt(profile, ["Мероприятия"], [])):
        assert "Киев" in prompt and "Другой город" not in prompt
        assert "Только сопровождение мероприятий" in prompt
        assert "По предварительной записи" in prompt
    shared_only = va_strategy.apply_strategy({}, {"enabled": True, "destination": "@manager",
                                                 "business": {"geography": "Другой город"}})
    assert "geography" not in shared_only["business"]


def test_geo_install_preserves_explicit_local_context():
    settings = geo_va_link.settings_for_target({"topic": "Мероприятия", "publish_mode": "review",
                                               "posts_per_day": 2}, {"city": "Киев", "country": "Украина"})
    assert settings["business"]["geography"] == "Киев, Украина"
