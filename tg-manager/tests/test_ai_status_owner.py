"""Регресс (PRODUCT_DEPTH_PLAN P1): владелец бота видит статус AI.

Было: «Статус AI» только у админа (admin._adm_ai_status); у владельца бота при
сбое ИИ-автоответчика — тишина (ошибка только в лог). Добавлены owner-scoped
`GET /api/miniapp/ai/status` (какие провайдеры настроены) и
`POST /api/miniapp/ai/test` (live-пинг, реюз ai_providers.ping_providers) + блок
«Статус AI» на экране авто-ответов.
"""
from __future__ import annotations

import inspect
import os
import re

from services import ai_providers, mini_app_api

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")


def test_ping_providers_helper_exists_and_shape():
    assert hasattr(ai_providers, "ping_providers"), "нужен общий хелпер ping_providers"
    src = inspect.getsource(ai_providers.ping_providers)
    assert "configured_providers()" in src, "ping_providers должен реюзать configured_providers"
    assert '"ok"' in src and '"ms"' in src and '"name"' in src, "shape [{name,ok,ms}]"
    # не бросает — сбои инкапсулированы в ok=False
    assert "except Exception" in src


def test_owner_endpoints_registered_and_scoped():
    src = inspect.getsource(mini_app_api)
    assert re.search(r'add_get\(\s*"/api/miniapp/ai/status"\s*,\s*ai_status', src)
    assert re.search(r'add_post\(\s*"/api/miniapp/ai/test"\s*,\s*ai_test', src)
    for handler in ("ai_status", "ai_test"):
        m = re.search(rf"async def {handler}\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
        assert m, f"handler {handler} not found"
        body = m.group(1)
        assert "_get_uid(request)" in body and "Unauthorized" in body, (
            f"{handler} должен быть owner-scoped (требовать uid)"
        )
    # ai_status реюзает configured_providers, ai_test — ping_providers
    m = re.search(r"async def ai_status\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert "configured_providers" in m.group(1)
    m = re.search(r"async def ai_test\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert "ping_providers" in m.group(1)


def test_ui_ai_status_block_wired():
    html = open(_HTML, encoding="utf-8").read()
    assert 'id="aiStatusBox"' in html, "нет контейнера статуса AI на экране авто-ответов"
    for fn in ("function loadAiStatus", "async function testAi"):
        assert fn in html, f"нет JS {fn}"
    # экран авто-ответов подгружает статус AI при открытии
    m = re.search(r"async function openArScreen\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m and "loadAiStatus()" in m.group(1), "openArScreen должен вызывать loadAiStatus()"
    # вызовы идут на owner-эндпоинты
    assert "'/api/miniapp/ai/status'" in html and "'/api/miniapp/ai/test'" in html
