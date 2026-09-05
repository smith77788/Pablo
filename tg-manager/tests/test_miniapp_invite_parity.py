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
    # пре-флайт использует общий сервис (тот же, что бот)
    assert "from services.invite_preflight import run_preflight" in api
    # «вступить всеми» — фоновая операция bulk_join (не инлайн: живой join
    # флотом дольше таймаута шлюза), ставится через общий operation_bus
    assert '"bulk_join"' in api
    assert "from services import operation_bus" in api


def test_frontend_has_controls_and_sends_params():
    from tests.miniapp_source import miniapp_source
    html = miniapp_source()
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
    from tests.miniapp_source import miniapp_source
    html = miniapp_source()
    assert "function loadFleetReadiness" in html
    assert "function joinAllToGroup" in html
    assert "/api/miniapp/invite/fleet_readiness" in html
    assert "/api/miniapp/invite/join_all" in html


def test_backend_file_upload_and_dedup():
    api = _read("services/mini_app_api.py")
    # эндпоинт загрузки файла + маршрут
    assert "async def invite_parse_file" in api
    assert 'add_post("/api/miniapp/invite/parse_file"' in api
    # multipart-разбор и общий парсер файла
    assert "await request.multipart()" in api
    assert "from services.invite_list_parser import extract_text" in api
    # превью дедупа в parse_list + submit принимает массивы из файла
    assert "already_invited" in api
    assert "count_already_invited" in api
    assert 'isinstance(_ur_in, list)' in api


def test_frontend_file_upload_and_dedup():
    from tests.miniapp_source import miniapp_source
    html = miniapp_source()
    assert 'id="massInviteFile"' in html
    assert "function uploadInviteFile" in html
    assert "/api/miniapp/invite/parse_file" in html
    assert "INV_FILE_REFS" in html and "INV_FILE_PHONES" in html
    # api() умеет FormData (не форсит JSON Content-Type)
    assert "instanceof FormData" in html
    # дедуп-строка в превью
    assert "already_invited" in html


def test_count_already_invited_service():
    # общий сервисный хелпер дедупа (используется и ботом, и Mini App)
    from services import invite_preflight
    assert hasattr(invite_preflight, "count_already_invited")
