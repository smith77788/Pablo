"""Токен сессии мини-аппа не ходит в адресе.

ЧТО БЫЛО. `_get_uid` принимал токен и из заголовка, и параметром `?token=`.
Параметром пользовались четыре места: живой поток (EventSource не умеет
заголовки), общая выгрузка CSV и выгрузка аудитории (обе открываются во
ВНЕШНЕМ браузере через `tg.openLink`) и нативное сохранение файла
(`tg.downloadFile`). Строка запроса оседает в журнале доступа aiohttp, в логах
платформы и в истории внешнего браузера — то есть двухчасовой ключ ко ВСЕМ
данным владельца лежал там, куда владелец не заглядывает. SECURITY.md: «❌
Токены в коде/URL».

ЧТО СТАЛО. Токен сессии — только из заголовка. Ссылке без заголовка выдаётся
пропуск (`POST /api/miniapp/url_ticket`): подписан с другой меткой, привязан к
одному пути, годится только для GET и живёт две минуты (поток — как сессия,
потому что EventSource переподключается сам тем же адресом).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from services import mini_app_api as M
from services import mini_app_auth as A

ROOT = Path(__file__).resolve().parents[1]
BOT = "123456789:AAEhBP0abcdefghijklmnopqrstuvwxyz12345678"
UID = 424242
EXPORT = "/api/miniapp/parser/audience/export"


class _Req:
    def __init__(self, path, method="GET", query=None, headers=None):
        self.path, self.method = path, method
        self.query = query or {}
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", BOT)


def test_header_token_still_works():
    tok = A.make_token(UID, BOT)
    assert M._get_uid(_Req(EXPORT, headers={"Authorization": "Bearer " + tok})) == UID


def test_session_token_in_query_is_refused():
    tok = A.make_token(UID, BOT)
    assert M._get_uid(_Req(EXPORT, query={"token": tok})) is None


def test_ticket_opens_only_its_path_and_only_get():
    t = A.make_url_ticket(UID, BOT, EXPORT)
    assert M._get_uid(_Req(EXPORT, query={"ticket": t})) == UID
    assert M._get_uid(_Req("/api/miniapp/accounts/export", query={"ticket": t})) is None
    assert M._get_uid(_Req(EXPORT, method="POST", query={"ticket": t})) is None


def test_ticket_is_not_a_session_token_and_vice_versa():
    t = A.make_url_ticket(UID, BOT, EXPORT)
    assert A.parse_token(t, BOT) is None
    assert A.parse_url_ticket(A.make_token(UID, BOT), BOT, EXPORT) is None


def test_forged_ticket_for_other_user_is_refused():
    uid_s, ts, sig = A.make_url_ticket(UID, BOT, EXPORT).split(".")
    assert A.parse_url_ticket(f"{UID + 1}.{ts}.{sig}", BOT, EXPORT) is None


def test_download_ticket_dies_fast_stream_ticket_lives_like_session(monkeypatch):
    t = A.make_url_ticket(UID, BOT, EXPORT)
    s = A.make_url_ticket(UID, BOT, A.EVENTS_PATH)
    later = time.time() + A.URL_TICKET_TTL + 5
    monkeypatch.setattr(A.time, "time", lambda: later)
    assert A.parse_url_ticket(t, BOT, EXPORT) is None
    assert A.parse_url_ticket(s, BOT, A.EVENTS_PATH) == UID
    assert A.EVENTS_TICKET_TTL <= 7200   # не дольше токена сессии


def test_frontend_never_puts_the_session_token_in_a_url():
    sources = [ROOT / "mini_app" / "index.html", *(ROOT / "mini_app" / "screens").glob("*.js")]
    bad = []
    for p in sources:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""token=['"]?\s*\+\s*encodeURIComponent\(TK\)|[?&]token=|\.set\(\s*['"]token['"]\s*,\s*TK""", line):
                bad.append(f"{p.name}:{i}: {line.strip()[:90]}")
    assert not bad, bad


def test_frontend_links_use_tickets():
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "async function urlTicket(path)" in html
    assert "events?ticket=" in html
    for fn in ("exportCSV", "exportAudience"):
        body = html[html.index(f"function {fn}("):][:400]
        assert "openTicketLink(" in body, fn


def test_ticket_route_is_registered():
    from aiohttp import web

    app = web.Application()

    class _P:
        async def fetch(self, *a): return []
        async def fetchrow(self, *a): return None
        async def fetchval(self, *a): return 0
        async def execute(self, *a): return "OK"

    M.setup_routes(app, _P())
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()
              if r.resource is not None}
    assert ("POST", "/api/miniapp/url_ticket") in routes
