"""Личные API-ключи принимаются REST API и ограничены своим владельцем.

РЕГРЕСС. Бот («API Hub») и мини-апп («API Ключи») выдавали ключи, а
`rest_api._check_auth` принимал только общий ADMIN_SECRET: владелец с ключом из
мини-аппа получал 401 на любой запрос. Здесь проверяется, что выданный ключ
входит, что он видит только своего владельца и что ключ «только чтение» не
может писать от имени аккаунтов.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from services import api_keys, rest_api


class _KeyPool:
    """Пул, который помнит выданные ключи — ровно настолько, чтобы прогнать
    выдачу и вход через настоящие запросы модуля."""

    def __init__(self):
        self.rows: list[dict] = []
        self.ops: list[dict] = []

    async def fetchval(self, q, *a):
        if "COUNT(*) FROM api_keys" in q:
            return sum(1 for r in self.rows if r["user_id"] == a[0] and r["is_active"])
        return 0

    async def fetchrow(self, q, *a):
        if q.lstrip().startswith("INSERT INTO api_keys"):
            row = {"id": len(self.rows) + 1, "user_id": a[0], "key_hash": a[1],
                   "key_prefix": a[2], "name": a[3], "scopes": list(a[4]),
                   "is_active": True, "requests_total": 0}
            self.rows.append(row)
            return {"id": row["id"]}
        if q.lstrip().startswith("UPDATE api_keys"):
            for r in self.rows:
                if r["key_hash"] == a[0] and r["is_active"]:
                    r["requests_total"] += 1
                    return {"id": r["id"], "user_id": r["user_id"], "scopes": r["scopes"]}
            return None
        if "FROM operation_queue WHERE id=$1 AND owner_id=$2" in q:
            for o in self.ops:
                if o["id"] == a[0] and o["owner_id"] == a[1]:
                    return o
            return None
        return None

    async def fetch(self, q, *a):
        return []

    async def execute(self, q, *a):
        if "SET is_active=FALSE" in q:
            for r in self.rows:
                if r["id"] == a[0] and r["user_id"] == a[1] and r["is_active"]:
                    r["is_active"] = False
                    return "UPDATE 1"
            return "UPDATE 0"
        return "OK"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _app(pool):
    app = web.Application()
    rest_api.add_routes(app, pool, bot=None)
    return app


async def _call(app, method, path, headers=None, match=None):
    req = make_mocked_request(method, path, headers=headers or {}, app=app,
                              match_info=match or {})
    res = await app.router.resolve(req)
    handler = res.handler
    if match:
        return await handler(req)
    req = make_mocked_request(method, path, headers=headers or {}, app=app,
                              match_info=dict(res))
    return await handler(req)


@pytest.fixture(autouse=True)
def _no_admin_secret(monkeypatch):
    monkeypatch.setattr(rest_api, "ADMIN_SECRET", "служебный", raising=False)


def test_issued_key_is_accepted_and_names_its_owner():
    pool = _KeyPool()
    k = _run(api_keys.create_key(pool, 111, "n8n"))
    assert k["key"].startswith("bm_") and k["prefix"] == k["key"][:11]
    app = _app(pool)
    resp = _run(_call(app, "GET", "/api/v1/me", {"X-Api-Key": k["key"]}))
    assert resp.status == 200, resp.text
    import json
    body = json.loads(resp.text)
    assert body["owner_id"] == 111 and body["admin"] is False
    assert body["scopes"] == ["read", "write"]


def test_bearer_header_works_too():
    pool = _KeyPool()
    k = _run(api_keys.create_key(pool, 5, "x"))
    resp = _run(_call(_app(pool), "GET", "/api/v1/me",
                      {"Authorization": "Bearer " + k["key"]}))
    assert resp.status == 200


def test_unknown_and_revoked_keys_are_refused():
    pool = _KeyPool()
    k = _run(api_keys.create_key(pool, 7, "x"))
    app = _app(pool)
    assert _run(_call(app, "GET", "/api/v1/me", {"X-Api-Key": "bm_нет"})).status == 401
    assert _run(api_keys.revoke_key(pool, 7, k["id"]))
    assert _run(_call(app, "GET", "/api/v1/me", {"X-Api-Key": k["key"]})).status == 401


def test_key_cannot_read_another_owner():
    pool = _KeyPool()
    k = _run(api_keys.create_key(pool, 7, "x"))
    resp = _run(_call(_app(pool), "GET", "/api/v1/accounts?owner_id=8",
                      {"X-Api-Key": k["key"]}))
    assert resp.status == 403


def test_operation_of_another_owner_is_not_found():
    pool = _KeyPool()
    pool.ops.append({"id": 50, "owner_id": 8, "op_type": "broadcast", "status": "done"})
    k = _run(api_keys.create_key(pool, 7, "x"))
    resp = _run(_call(_app(pool), "GET", "/api/v1/operations/50",
                      {"X-Api-Key": k["key"]}, match={"op_id": "50"}))
    assert resp.status == 404


def test_read_only_key_cannot_send_messages():
    pool = _KeyPool()
    k = _run(api_keys.create_key(pool, 7, "отчёты", ["read"]))
    assert k["scopes"] == ["read"]
    resp = _run(_call(_app(pool), "POST", "/api/v1/send_message", {"X-Api-Key": k["key"]}))
    assert resp.status == 403
    assert "write" in resp.text


def test_admin_secret_still_works_but_needs_owner():
    pool = _KeyPool()
    app = _app(pool)
    resp = _run(_call(app, "GET", "/api/v1/accounts", {"X-Api-Key": "служебный"}))
    assert resp.status == 400


def test_resolve_owner_rules():
    key = {"admin": False, "owner_id": 10}
    admin = {"admin": True, "owner_id": None}
    assert rest_api.resolve_owner(key, None) == (10, None)
    assert rest_api.resolve_owner(key, "10") == (10, None)
    assert rest_api.resolve_owner(key, 11) == (None, "forbidden_owner")
    assert rest_api.resolve_owner(key, "abc")[0] is None
    assert rest_api.resolve_owner(admin, None) == (None, "owner_id required")
    assert rest_api.resolve_owner(admin, 12) == (12, None)


def test_scopes_normalization():
    assert api_keys.normalize_scopes(None) == ["read", "write"]
    assert api_keys.normalize_scopes(["read"]) == ["read"]
    assert api_keys.normalize_scopes(["write"]) == ["read", "write"], "write без read не бывает"
    assert api_keys.normalize_scopes("мусор") == ["read", "write"]


def test_key_limit_is_enforced_in_one_place():
    pool = _KeyPool()
    for i in range(api_keys.MAX_ACTIVE_KEYS):
        _run(api_keys.create_key(pool, 3, f"k{i}"))
    with pytest.raises(ValueError):
        _run(api_keys.create_key(pool, 3, "лишний"))


def test_bot_and_miniapp_issue_keys_through_the_same_door():
    """Две выдачи с разной логикой — и привели к ключам, которые нигде не работали."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot = open(os.path.join(root, "bot", "handlers", "api_hub.py"), encoding="utf-8").read()
    app = open(os.path.join(root, "services", "mini_app_integrations.py"), encoding="utf-8").read()
    assert "_keys.create_key(" in bot and "INSERT INTO api_keys" not in bot
    assert "_keys.create_key(" in app and "INSERT INTO api_keys" not in app
