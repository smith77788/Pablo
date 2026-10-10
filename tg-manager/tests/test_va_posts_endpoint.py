"""Посты виртуального администратора: переход к посту В КАНАЛЕ.

Жалоба владельца: «не видно, что публиковал вирт.админ, и нельзя нажать на
пост и перейти к нему в канале». Проверяем построение ссылки (unit) и то, что
запрос сшивает пост с каналом и даёт ссылку (e2e на живом Postgres).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services.mini_app_api import va_post_link


# ── Фронт: экран постов и переход в канал ─────────────────────────────────────

def test_frontend_posts_screen_links_to_channel():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent
            / "mini_app" / "index.html").read_text(encoding="utf-8")
    # экран, вход и функции на месте
    assert 'id="s-va-posts"' in html, "нет экрана постов вирт.админа"
    assert 'onclick="openVaPosts()"' in html, "нет кнопки входа в посты"
    assert "async function openVaPosts(" in html
    assert "function openTgLink(" in html, "нет хелпера открытия t.me-ссылки"
    # эндпоинт и переход к посту в канале
    assert "/api/miniapp/va/posts" in html
    assert "openTgLink('" in html or "openTgLink(`" in html, \
        "строка поста не ведёт в канал через openTgLink"
    # роут зарегистрирован на бэкенде
    api = (pathlib.Path(__file__).resolve().parent.parent
           / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert '"/api/miniapp/va/posts"' in api and "va_posts)" in api


# ── Unit: построение ссылки на пост ───────────────────────────────────────────

def test_public_channel_link():
    link, private = va_post_link("mychan", -1001234567890, 55)
    assert link == "https://t.me/mychan/55"
    assert private is False


def test_private_channel_link_strips_peer_prefix():
    # -100XXXXXXXXXX → внутренний XXXXXXXXXX для t.me/c/
    link, private = va_post_link(None, -1001234567890, 55)
    assert link == "https://t.me/c/1234567890/55"
    assert private is True


def test_no_msg_id_no_link():
    assert va_post_link("mychan", -1001234567890, None) == (None, False)


def test_private_without_channel_id_is_markable():
    link, private = va_post_link(None, None, 55)
    assert link is None and private is True


# ── E2e: запрос сшивает пост с каналом ────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
e2e = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

_SQL = """
SELECT p.id, p.channel_key, p.msg_id, p.body,
       mc.title AS ch_title, mc.username AS ch_username, mc.channel_id AS ch_id
FROM va_channel_posts p
LEFT JOIN managed_channels mc ON mc.owner_id = p.owner_id
     AND (p.channel_key = mc.channel_id::text
          OR p.channel_key = mc.username
          OR p.channel_key = '@' || mc.username)
WHERE p.owner_id = $1
ORDER BY p.published_at DESC, p.id DESC LIMIT 50
"""


@e2e
def test_posts_join_channel_and_build_link():
    import asyncpg

    async def scenario():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        uid = 777042
        try:
            await pool.execute("DELETE FROM va_channel_posts WHERE owner_id=$1", uid)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", uid)
            await pool.execute(
                "INSERT INTO managed_channels(owner_id,acc_id,channel_id,title,username) "
                "VALUES($1,1,$2,'Канал','mychan')", uid, -1001234567890)
            await pool.execute(
                "INSERT INTO va_channel_posts(owner_id,channel_key,msg_id,body) "
                "VALUES($1,$2,$3,'тело')", uid, "-1001234567890", 55)
            # старый пост без msg_id — остаётся виден, но без ссылки
            await pool.execute(
                "INSERT INTO va_channel_posts(owner_id,channel_key,body) "
                "VALUES($1,$2,'старый')", uid, "-1001234567890")

            rows = await pool.fetch(_SQL, uid)
            assert len(rows) == 2
            by_msg = {r["msg_id"]: r for r in rows}
            # пост с msg_id сшит с каналом и даёт ссылку
            r = by_msg[55]
            assert r["ch_title"] == "Канал"
            link, priv = va_post_link(r["ch_username"], r["ch_id"], r["msg_id"])
            assert link == "https://t.me/mychan/55" and priv is False
            # пост без msg_id — ссылки нет, но строка на месте
            r0 = by_msg[None]
            assert va_post_link(r0["ch_username"], r0["ch_id"], r0["msg_id"])[0] is None
        finally:
            await pool.execute("DELETE FROM va_channel_posts WHERE owner_id=$1", uid)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", uid)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
