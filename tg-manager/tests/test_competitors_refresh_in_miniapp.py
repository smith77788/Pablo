"""Конкуренты в мини-аппе: обновление подписчиков, динамика, правка, пакет.

Обновление числа подписчиков жило только в боте, и на экране мини-аппа у
каждого конкурента навсегда стояло «не проверен». Теперь одна функция
`competitor_watch.refresh` на оба места, ручка /competitors/refresh, а экран
показывает динамику, сортирует, правит метку и добавляет пачкой.
"""
from __future__ import annotations

import asyncio
import os

from services import competitor_watch as cw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_members_parsed_from_tme_page():
    page = '<div class="tgme_page_extra">12 345 subscribers</div>'
    assert cw.members_from_page(page) == 12345
    assert cw.members_from_page("1\xa0204 members") == 1204
    assert cw.members_from_page("<html>nothing</html>") is None


class _Pool:
    def __init__(self):
        self.updates = []
        self.fetch_args = None

    async def fetch(self, q, *a):
        self.fetch_args = (q, a)
        return [{"id": 1, "username": "aaa"}, {"id": 2, "username": "bbb"}]

    async def execute(self, q, *a):
        self.updates.append((q, a))
        return "UPDATE 1"


def test_refresh_moves_last_to_prev_and_scopes_owner(monkeypatch):
    async def _fm(sess, username):
        return {"aaa": 100, "bbb": None}[username]

    monkeypatch.setattr(cw, "fetch_members", _fm)
    pool = _Pool()
    res = asyncio.run(cw.refresh(pool, 42, [1, 2], pause=0))
    assert res == {"total": 2, "updated": 1}
    q, a = pool.updates[0]
    assert "prev_members = last_members" in q and "owner_id = $3" in q
    assert a == (100, 1, 42)
    assert "owner_id=$1" in pool.fetch_args[0] and pool.fetch_args[1][0] == 42


def test_bot_and_miniapp_share_one_refresh():
    bot = _read("bot", "handlers", "competitors.py")
    assert "competitor_watch.refresh(" in bot
    assert "tgme_page_extra" not in bot, "разбор страницы остался копией в боте"
    from tests.miniapp_source import api_source

    api = api_source()
    assert '"/api/miniapp/competitors/refresh"' in api
    assert "competitor_watch.refresh(" in api


def test_screen_has_refresh_trend_edit_and_batch():
    html = _read("mini_app", "index.html")
    for needle in ("refreshComps()", "openCompEdit(${c.id})", "compToParser(${c.id})",
                   'id="compSort"', "c.prev_members"):
        assert needle in html, needle
    save = html[html.index("async function saveComp() {"):html.index("async function deleteComp(")]
    assert "for (const username of names)" in save
