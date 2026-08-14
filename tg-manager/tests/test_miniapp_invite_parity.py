"""Паритет инвайтера бот ↔ Mini App: способ инвайта, режим объёма, пре-флайт,
вступление всеми — всё, что есть в боте, должно быть и в Mini App.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_forwards_method_and_volume():
    api = _read("services/mini_app_api.py")
    # mass_inviter_submit прокидывает способ инвайта и режим объёма в params
    assert 'params["invite_method"]' in api
    assert 'params["volume_mode"] = "progressive"' in api
    # link_message для метода «ссылка в ЛС»
    assert 'params["link_message"]' in api


def test_backend_readiness_and_joinall_endpoints():
    api = _read("services/mini_app_api.py")
    assert "async def invite_fleet_readiness" in api
    assert "async def invite_join_all" in api
    assert 'add_get("/api/miniapp/invite/fleet_readiness"' in api
    assert 'add_post("/api/miniapp/invite/join_all"' in api
    # используют общий сервис (тот же, что бот)
    assert "from services.invite_preflight import run_preflight" in api
    assert "from services.invite_preflight import join_all" in api


def test_frontend_has_controls_and_sends_params():
    html = _read("mini_app/index.html")
    # селекторы способа и объёма
    assert 'id="massInviteMethod"' in html
    assert 'id="massInviteVolMode"' in html
    # значения методов — паритет с ботом
    for v in ('value="direct"', 'value="admin"', 'value="link"'):
        assert v in html
    assert 'value="progressive"' in html
    # тело запроса реально несёт эти поля
    assert "body.invite_method =" in html
    assert "body.volume_mode = 'progressive'" in html


def test_frontend_has_readiness_and_joinall():
    html = _read("mini_app/index.html")
    assert "function loadFleetReadiness" in html
    assert "function joinAllToGroup" in html
    assert "/api/miniapp/invite/fleet_readiness" in html
    assert "/api/miniapp/invite/join_all" in html
