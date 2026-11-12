"""Регрессия: масс-действия по всему серверному срезу (не только загруженной странице).

mini_app_api не импортируется в песочнице — проверяем на уровне исходников, что
accounts_mass резолвит весь набор через тот же owner/admin-скоуп WHERE
(_accounts_where) с лимитом-предохранителем, и что UI отправляет select_all_filtered
с текущим фильтром вместо списка id.
"""
from __future__ import annotations

import json
import os
import re
import pytest
from tests.miniapp_source import api_function, api_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_resolves_whole_filter_scoped():
    mass = api_function("accounts_mass")
    assert 'body.get("select_all_filtered")' in mass, "нет режима select_all_filtered"
    # использует тот же билдер WHERE со скоупом owner/admin — и с ТЕМИ ЖЕ
    # срезами, что видит владелец: фильтр, CRM-этап, поиск и пул. Пропущенный
    # срез здесь означает, что операция уйдёт шире показанного списка.
    # Вызов переносится на несколько строк, как только добавляется новый срез,
    # поэтому ищем с re.S: без этого проба перестаёт видеть здоровый код и
    # краснеет на правильной правке вместо того, чтобы ловить пропущенный срез.
    call = re.search(r"_accounts_where\((.*?)\)\n", mass, re.S)
    assert call, "accounts_mass не зовёт общий билдер WHERE"
    for part in ("uid", "flt", "stage", "qterm", "admin=admin", "acc_pool=", "geo="):
        assert part in call.group(1), f"в срезе масс-действия нет {part}"
    # предохранитель от неограниченного enqueue
    assert "LIMIT {_mass_cap() + 1}" in mass and "capped" in mass


def test_ui_sends_select_all_filtered():
    ui = _read("mini_app/index.html")
    m = re.search(r"function _massSel\(\)\s*\{(.*?)\n\}", ui, re.DOTALL)
    assert m, "_massSel не найден"
    body = m.group(1)
    assert "select_all_filtered: true" in body
    for part in ("filter: ACC_FILTER", "stage: ACC_STAGE_FILTER", "q: ACC_SEARCH",
                 "pool: ACC_POOL_FILTER", "geo: ACC_GEO_FILTER"):
        assert part in body, f"UI не передаёт {part} в масс-действие"
    # все масс-пути идут через _massSel (не жёстко account_ids)
    assert "op, ..._massSel()" in ui        # runAccMass
    assert "op:'set_stage', target_stage:stage, ..._massSel()" in ui
    assert "Object.assign(payload, _massSel())" in ui    # submitAccProfile


@pytest.fixture
def mass_endpoint(monkeypatch):
    from unittest.mock import AsyncMock

    from aiohttp import web
    from services import mini_app_accounts, mini_app_api

    pool = AsyncMock()
    pool.execute.return_value = "UPDATE 1"
    fetch = AsyncMock(return_value=[{"id": 5}, {"id": 6}])
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 42)
    monkeypatch.setattr(mini_app_api, "_safe_fetch", fetch)
    app = web.Application()
    mini_app_accounts.setup_routes(app, pool)
    route = next(r for r in app.router.routes()
                 if r.resource.canonical == "/api/miniapp/accounts/mass")

    async def post(body):
        from aiohttp.test_utils import make_mocked_request

        req = make_mocked_request("POST", route.resource.canonical)
        req.json = AsyncMock(return_value=body)
        return await route.handler(req)

    return post, fetch, pool


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_field,value", [("filter", "typo"), ("stage", "typo"),
                                               ("stage", 2), ("geo", "invalid")])
async def test_invalid_filter_never_reaches_selection(mass_endpoint, filter_field, value):
    post, fetch, pool = mass_endpoint
    response = await post({"op": "delete", "select_all_filtered": True, filter_field: value})
    assert response.status == 400
    fetch.assert_not_awaited()
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_filtered_accounts_keeps_source_and_destination_separate(mass_endpoint):
    post, fetch, pool = mass_endpoint
    response = await post({"op": "set_stage", "select_all_filtered": True,
                           "stage": "ready", "target_stage": "resting"})
    assert response.status == 200, response.text
    assert fetch.await_args.args[2:] == (42, "ready")
    assert pool.execute.await_args.args[1:] == ("resting", 42, [5, 6])
    assert json.loads(response.text)["count"] == 1


@pytest.mark.asyncio
async def test_move_selected_accounts_accepts_legacy_destination(mass_endpoint):
    post, _, pool = mass_endpoint
    response = await post({"op": "set_stage", "account_ids": [5, 6], "stage": "ready"})
    assert response.status == 200, response.text
    assert pool.execute.await_args.args[1:] == ("ready", 42, [5, 6])


@pytest.mark.asyncio
async def test_move_filtered_accounts_requires_explicit_destination(mass_endpoint):
    post, fetch, pool = mass_endpoint
    response = await post({"op": "set_stage", "select_all_filtered": True, "stage": "ready"})
    assert response.status == 400
    fetch.assert_not_awaited()
    pool.execute.assert_not_awaited()
