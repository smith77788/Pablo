"""Admin archive selection must not weaken Vault tenant isolation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from services import mini_app_api as api
from services import mini_app_vault

VIEWER = 71001
OTHER = 72002
HTML = (Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text(
    encoding="utf-8"
)


class _Pool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.fetches.append((query, args))
        return self.rows


class _Request:
    def __init__(self, *, owner_id=None, method="GET", match_info=None, body=None):
        self.query = {} if owner_id is None else {"owner_id": str(owner_id)}
        self.headers = {}
        self.method = method
        self.match_info = match_info or {}
        self.path = "/api/miniapp/vault/test"
        self._body = body or {}

    async def json(self):
        return self._body


def _handler(monkeypatch, pool, method: str, path: str, *, admin: bool):
    monkeypatch.setattr(api, "_get_uid", lambda request: VIEWER)
    monkeypatch.setattr(api, "_is_admin", lambda uid: admin and uid == VIEWER)
    app = web.Application()
    mini_app_vault.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        registered = info.get("path") or info.get("formatter") or ""
        if route.method == method and registered == path:
            return route.handler
    raise AssertionError(f"route not found: {method} {path}")


def _payload(response: web.Response) -> dict:
    return json.loads(response.text)


def test_regular_user_cannot_read_another_archive(monkeypatch):
    from services import vault_service

    called = False

    async def fake_list(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(vault_service, "list_chats", fake_list)
    handler = _handler(
        monkeypatch, _Pool(), "GET", "/api/miniapp/vault/chats", admin=False
    )
    response = asyncio.run(handler(_Request(owner_id=OTHER)))

    assert response.status == 403
    assert not called, "tenant check happened after reading another owner's archive"


@pytest.mark.parametrize(
    ("method", "path", "match_info"),
    [
        ("GET", "/api/miniapp/vault/status", {}),
        ("GET", "/api/miniapp/vault/analytics", {}),
        ("GET", "/api/miniapp/vault/chats", {}),
        ("GET", "/api/miniapp/vault/chat/{chat_id}/messages", {"chat_id": "1"}),
        ("GET", "/api/miniapp/vault/search", {}),
        ("GET", "/api/miniapp/vault/recent", {}),
        ("GET", "/api/miniapp/vault/export", {}),
        ("GET", "/api/miniapp/vault/settings", {}),
        (
            "GET",
            "/api/miniapp/vault/media/{chat_id}/{msg_id}",
            {"chat_id": "1", "msg_id": "2"},
        ),
    ],
)
def test_every_read_route_rejects_a_foreign_owner(
    monkeypatch, method, path, match_info
):
    handler = _handler(monkeypatch, _Pool(), method, path, admin=False)
    response = asyncio.run(
        handler(_Request(owner_id=OTHER, method=method, match_info=match_info))
    )
    assert response.status == 403, path


def test_admin_reads_exactly_the_selected_archive(monkeypatch):
    from services import vault_service

    owners = []

    async def fake_list(pool, owner_id, *, limit):
        owners.append(owner_id)
        return []

    monkeypatch.setattr(vault_service, "list_chats", fake_list)
    handler = _handler(
        monkeypatch, _Pool(), "GET", "/api/miniapp/vault/chats", admin=True
    )
    response = asyncio.run(handler(_Request(owner_id=OTHER)))

    assert response.status == 200
    assert owners == [OTHER]


def test_missing_owner_id_stays_owner_scoped_even_for_admin(monkeypatch):
    from services import vault_service

    owners = []

    async def fake_list(pool, owner_id, *, limit):
        owners.append(owner_id)
        return []

    monkeypatch.setattr(vault_service, "list_chats", fake_list)
    handler = _handler(
        monkeypatch, _Pool(), "GET", "/api/miniapp/vault/chats", admin=True
    )
    response = asyncio.run(handler(_Request()))

    assert response.status == 200
    assert owners == [VIEWER], "admin scope must be an explicit opt-in"


@pytest.mark.parametrize("owner_id", [0, -1, "bad", 9_223_372_036_854_775_808])
def test_invalid_owner_id_never_reaches_storage(monkeypatch, owner_id):
    from services import vault_service

    called = False

    async def fake_list(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(vault_service, "list_chats", fake_list)
    handler = _handler(
        monkeypatch, _Pool(), "GET", "/api/miniapp/vault/chats", admin=True
    )
    response = asyncio.run(handler(_Request(owner_id=owner_id)))

    assert response.status == 400
    assert not called


def test_owner_selector_is_admin_only(monkeypatch):
    path = "/api/miniapp/vault/owners"
    denied = _handler(monkeypatch, _Pool(), "GET", path, admin=False)
    assert asyncio.run(denied(_Request())).status == 403

    rows = [{
        "owner_id": VIEWER,
        "username": "admin",
        "first_name": "Admin",
        "chats": 2,
        "messages": 9,
        "connected": True,
        "last_activity": None,
    }]
    pool = _Pool(rows)
    allowed = _handler(monkeypatch, pool, "GET", path, admin=True)
    response = asyncio.run(allowed(_Request()))

    assert response.status == 200
    assert _payload(response)["owners"][0]["owner_id"] == VIEWER
    assert pool.fetches[0][1] == (VIEWER,)


def test_admin_cannot_act_as_the_selected_owner(monkeypatch):
    reply = _handler(
        monkeypatch,
        _Pool(),
        "POST",
        "/api/miniapp/vault/chat/{chat_id}/reply",
        admin=True,
    )
    response = asyncio.run(
        reply(
            _Request(
                owner_id=OTHER,
                method="POST",
                match_info={"chat_id": "123"},
                body={"text": "should not be sent"},
            )
        )
    )
    assert response.status == 403

    settings = _handler(
        monkeypatch,
        _Pool(),
        "POST",
        "/api/miniapp/vault/settings",
        admin=True,
    )
    response = asyncio.run(
        settings(
            _Request(
                owner_id=OTHER,
                method="POST",
                body={"notify_deleted": False},
            )
        )
    )
    assert response.status == 403


def test_frontend_keeps_selected_owner_on_every_read_path():
    assert 'id="vaultOwnerBar"' in HTML
    assert 'id="vaultOwnerSelect"' in HTML
    assert "function vaultPath(path)" in HTML
    assert "VAULT_READ_ONLY" in HTML
    for endpoint in (
        "/api/miniapp/vault/status",
        "/api/miniapp/vault/chats",
        "/api/miniapp/vault/analytics",
        "/api/miniapp/vault/chat/",
        "/api/miniapp/vault/media/",
        "/api/miniapp/vault/search",
        "/api/miniapp/vault/recent",
        "/api/miniapp/vault/export",
    ):
        assert f"vaultPath('{endpoint}" in HTML, endpoint
