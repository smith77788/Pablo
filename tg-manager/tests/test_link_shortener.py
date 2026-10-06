"""Сокращатель ссылок: нормализация URL, кастомные коды, резолв с учётом кликов.

Владелец: «важно, чтобы я мог вставить свою длинную ссылку и получить короткую
вида infragram.app/link» — то есть кастомный код («link») и свой домен.
"""
from __future__ import annotations

import asyncio

from services import link_shortener as ls


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePool:
    """Хранит short_links в словаре и отвечает на используемые запросы."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def fetchrow(self, q, *a):
        if "INSERT INTO short_links" in q:
            code, owner, url, title = a
            if code in self.rows:
                return None  # ON CONFLICT DO NOTHING
            self.rows[code] = {"code": code, "owner_id": int(owner), "target_url": url,
                               "title": title, "clicks": 0, "disabled": False,
                               "created_at": None, "last_click_at": None}
            return {"code": code}
        if "SELECT code, clicks, title FROM short_links" in q:
            owner, url = a
            for r in self.rows.values():
                if r["owner_id"] == int(owner) and r["target_url"] == url and not r["disabled"]:
                    return {"code": r["code"], "clicks": r["clicks"], "title": r["title"]}
            return None
        if "UPDATE short_links SET clicks=clicks+1" in q:
            r = self.rows.get(a[0])
            if r and not r["disabled"]:
                r["clicks"] += 1
                return {"target_url": r["target_url"]}
            return None
        if "SELECT target_url FROM short_links" in q:
            r = self.rows.get(a[0])
            return {"target_url": r["target_url"]} if r and not r["disabled"] else None
        return None

    async def fetch(self, q, *a):
        return [dict(r) for r in self.rows.values() if r["owner_id"] == int(a[0])]

    async def execute(self, q, *a):
        if "SET disabled=" in q:
            code, owner, dis = a
            r = self.rows.get(code)
            if r and r["owner_id"] == int(owner):
                r["disabled"] = bool(dis)
                return "UPDATE 1"
            return "UPDATE 0"
        if "DELETE FROM short_links" in q:
            code, owner = a
            r = self.rows.get(code)
            if r and r["owner_id"] == int(owner):
                del self.rows[code]
                return "DELETE 1"
            return "DELETE 0"
        return "OK"


# ── нормализация URL ─────────────────────────────────────────────────────────

def test_normalize_url_adds_https_and_rejects_bad_schemes():
    assert ls.normalize_url("t.me/foo") == "https://t.me/foo"
    assert ls.normalize_url("https://example.com/x") == "https://example.com/x"
    assert ls.normalize_url("http://example.com") == "http://example.com"
    for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "", "   "):
        assert ls.normalize_url(bad) is None, bad


def test_normalize_url_blocks_self_redirect_loop():
    # Цель = наш же короткий редирект → петля, запрещаем.
    assert ls.normalize_url("https://infragram.app/s/abc",
                            self_hosts={"infragram.app"}) is None
    # но обычная ссылка на тот же домен (не /s/) допустима
    assert ls.normalize_url("https://infragram.app/promo",
                            self_hosts={"infragram.app"}) == "https://infragram.app/promo"


# ── кастомные коды ───────────────────────────────────────────────────────────

def test_custom_code_validation():
    assert ls.valid_custom_code("link")       # пример владельца — разрешён
    assert ls.valid_custom_code("my_promo-7")
    assert not ls.valid_custom_code("api")     # зарезервирован
    assert not ls.valid_custom_code("ab cd")   # пробел
    assert not ls.valid_custom_code("-start")  # начинается не с буквы/цифры
    assert not ls.valid_custom_code("")        # пусто


# ── create / resolve ─────────────────────────────────────────────────────────

def test_create_auto_code_then_resolve_counts_click():
    pool = _FakePool()
    res = _run(ls.create(pool, 42, "example.com/landing"))
    assert res["ok"] and res["code"]
    code = res["code"]
    # резолв отдаёт нормализованный URL и считает клик
    assert _run(ls.resolve(pool, code)) == "https://example.com/landing"
    assert _run(ls.resolve(pool, code)) == "https://example.com/landing"
    assert pool.rows[code]["clicks"] == 2


def test_create_dedups_same_target_for_owner():
    pool = _FakePool()
    a = _run(ls.create(pool, 42, "https://t.me/chan"))
    b = _run(ls.create(pool, 42, "https://t.me/chan"))
    assert a["code"] == b["code"], "один и тот же URL владельца → один код"
    assert b.get("existing")


def test_custom_code_link_works_end_to_end():
    pool = _FakePool()
    res = _run(ls.create(pool, 42, "https://very-long-url.example.com/a/b/c?x=1",
                         custom_code="link"))
    assert res["ok"] and res["code"] == "link"
    assert _run(ls.resolve(pool, "link")) == "https://very-long-url.example.com/a/b/c?x=1"


def test_custom_code_collision_rejected():
    pool = _FakePool()
    assert _run(ls.create(pool, 42, "https://a.com", custom_code="promo"))["ok"]
    r2 = _run(ls.create(pool, 99, "https://b.com", custom_code="promo"))
    assert not r2["ok"] and "занят" in r2["error"]


def test_reserved_code_never_resolves():
    pool = _FakePool()
    # даже если как-то оказался бы в БД, резолв зарезервированного отдаёт None
    assert _run(ls.resolve(pool, "api")) is None
    assert _run(ls.resolve(pool, "miniapp")) is None


def test_disabled_link_does_not_resolve():
    pool = _FakePool()
    code = _run(ls.create(pool, 42, "https://a.com"))["code"]
    assert _run(ls.set_disabled(pool, 42, code, True))
    assert _run(ls.resolve(pool, code)) is None


def test_delete_scoped_to_owner():
    pool = _FakePool()
    code = _run(ls.create(pool, 42, "https://a.com"))["code"]
    assert not _run(ls.delete(pool, 999, code)), "чужой владелец не удаляет"
    assert _run(ls.delete(pool, 42, code))
    assert _run(ls.resolve(pool, code)) is None


def test_list_scoped_and_shaped():
    pool = _FakePool()
    _run(ls.create(pool, 42, "https://a.com", custom_code="one"))
    _run(ls.create(pool, 42, "https://b.com", custom_code="two"))
    _run(ls.create(pool, 7, "https://c.com", custom_code="xxx"))
    items = _run(ls.list_for_owner(pool, 42))
    assert {i["code"] for i in items} == {"one", "two"}
    assert all("clicks" in i and "target_url" in i for i in items)


# ── роуты зарегистрированы и редирект работает ──────────────────────────────

class _Req:
    def __init__(self, match_info=None):
        self.match_info = match_info or {}
        self.headers: dict[str, str] = {}


def _route_paths(app):
    out = set()
    for r in app.router.routes():
        info = r.resource.get_info() if r.resource else {}
        out.add(info.get("formatter") or info.get("path"))
    return out


def test_routes_registered_and_redirect_works():
    from aiohttp import web
    from services import mini_app_api

    pool = _FakePool()
    _run(ls.create(pool, 42, "https://t.me/target", custom_code="link"))

    app = web.Application()
    mini_app_api.setup_routes(app, pool)

    paths = _route_paths(app)
    assert "/s/{code}" in paths, "короткий редирект /s/<code> не зарегистрирован"
    assert "/api/miniapp/links/create" in paths
    assert "/api/miniapp/links" in paths
    assert any(p and p.startswith("/{code") for p in paths), (
        "красивый корневой редирект /<code> не зарегистрирован")

    handler = None
    for r in app.router.routes():
        info = r.resource.get_info() if r.resource else {}
        if r.method == "GET" and (info.get("formatter") or info.get("path")) == "/s/{code}":
            handler = r.handler
    assert handler is not None

    # известный код → 302 на цель
    import pytest as _pt
    with _pt.raises(web.HTTPFound) as exc:
        _run(handler(_Req(match_info={"code": "link"})))
    assert exc.value.location == "https://t.me/target"

    # неизвестный код → 404 (а не падение)
    resp = _run(handler(_Req(match_info={"code": "nope404zz"})))
    assert resp.status == 404
