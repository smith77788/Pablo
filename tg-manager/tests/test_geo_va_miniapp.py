"""Global Presence Mini App creates and repairs VA-enabled networks."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from services import mini_app_api as api


OWNER = 414141


class _Pool:
    def __init__(self):
        self.calls = []
        self.plan = None

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return [{"id": 7}]

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "global_presence_plans" in query:
            return self.plan
        if "AS eligible" in query:
            return {"eligible": 2, "installed": 1, "enabled": 1}
        return {"done": 2, "with_avatar": 0, "with_username": 1, "applicable": 2}

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return 93

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"


class _Request:
    def __init__(self, body=None, plan_id="93"):
        self._body = body or {}
        self.match_info = {"plan_id": plan_id}
        self.headers = {}
        self.query = {}
        self.query_string = ""
        self.method = "POST"
        self.rel_url = type("Url", (), {"query": {}})()

    async def json(self):
        return self._body


def _route(pool, method, path):
    app = web.Application()
    api.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        candidate = info.get("path") or info.get("formatter") or ""
        if route.method == method and candidate == path:
            return route.handler
    raise AssertionError(f"Missing route: {method} {path}")


def _data(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(api, "_get_uid", lambda request: OWNER)
    api._cache.clear()
    yield
    api._cache.clear()


def _cities(count):
    return "\n".join(f"Town{i}, Ukraine, ua, Kyiv, uk, Europe/Kyiv" for i in range(count))


def _payload(count):
    return {"asset_type": "channel", "name_pattern": "Новости {{CITY_NAME}}",
            "username_pattern": "news_{{CITY_SLUG}}", "custom_cities": _cities(count),
            "account_ids": [7],
            "virtual_admin": {"enabled": True, "topic": "Новости",
                              "lead_contact": "@main", "publish_mode": "review"}}


def test_create_keeps_all_500_targets_and_va_settings():
    pool = _Pool()
    handler = _route(pool, "POST", "/api/miniapp/global_presence")
    with patch("database.db.create_global_presence_targets", new=AsyncMock(return_value=500)) as insert:
        response = asyncio.run(handler(_Request(_payload(500))))
    assert response.status == 200
    assert _data(response)["targets"] == 500
    assert len(insert.await_args.args[2]) == 500
    plan_insert = next(args for query, args in pool.calls if "INSERT INTO global_presence_plans" in query)
    selection = json.loads(plan_insert[4])
    assert selection["count"] == 500
    assert selection["virtual_admin"]["lead_contact"] == "@main"


def test_create_rejects_501_instead_of_silent_truncation():
    pool = _Pool()
    handler = _route(pool, "POST", "/api/miniapp/global_presence")
    response = asyncio.run(handler(_Request(_payload(501))))
    assert response.status == 400
    assert not pool.calls


def test_retry_requires_owner_plan_and_uses_saved_config():
    pool = _Pool()
    pool.plan = {"geo_selection": json.dumps({"virtual_admin": {
        "enabled": True, "topic": "Новости", "publish_mode": "review",
        "posts_per_day": 2, "lead_contact": "@main"}})}
    handler = _route(pool, "POST", "/api/miniapp/global_presence/{plan_id}/virtual_admin/retry")
    with patch("services.geo_va_link.retry_missing", new=AsyncMock(return_value={
        "checked": 2, "installed": 1, "failed": 1, "more": False})) as retry:
        response = asyncio.run(handler(_Request()))
    assert response.status == 200
    assert retry.await_args.args[:3] == (pool, OWNER, 93)
    assert retry.await_args.args[3]["lead_contact"] == "@main"
    assert any("owner_id=$2" in q and args == (93, OWNER) for q, args in pool.calls)


def test_detail_reports_actual_va_coverage():
    pool = _Pool()
    pool.plan = {"id": 93, "geo_selection": {"virtual_admin": {"enabled": True, "topic": "Новости"}}}
    handler = _route(pool, "GET", "/api/miniapp/global_presence/{plan_id}")
    response = asyncio.run(handler(_Request()))
    assert response.status == 200
    assert _data(response)["virtual_admin_coverage"] == {
        "eligible": 2, "installed": 1, "enabled": 1}
