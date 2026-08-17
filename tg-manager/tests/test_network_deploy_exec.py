"""Связки Фаза 2: исполнитель развёртывания (op deploy_network) — wiring/контракт."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ow():
    return open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()


def _api():
    return open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def test_op_dispatch_wired():
    src = _ow()
    assert 'op_type == "deploy_network"' in src
    assert "async def _exec_deploy_network" in src


def test_executor_creates_persists_and_wires():
    src = _ow()
    i = src.index("async def _exec_deploy_network")
    fn = src[i:i + 6000]
    # создаёт объекты через фабрику аккаунта
    assert "account_manager.create_channel" in fn
    # пишет id обратно в узел
    assert "update_node_status" in fn and "ref_id=ch_id" in fn
    # вяжет admin-рёбра
    assert "promote_to_admin" in fn
    # темп под губернатором
    assert "_governed_sleep" in fn
    # уважает отмену
    assert "_is_cancelled" in fn
    # событие в организм
    assert '"network_deployed"' in fn
    # боты — вручную (не автосоздаём)
    assert "BotFather" in fn


def test_deploy_endpoint_goes_through_bus():
    src = _api()
    i = src.index("async def network_deploy(")
    fn = src[i:i + 1800]
    assert 'operation_bus.submit' in fn
    assert '"deploy_network"' in fn
    assert "plan_deployment" in fn        # пусто → 400
    assert '"/api/miniapp/networks/{net_id}/deploy"' in src


def test_frontend_deploy_button():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function deployNet" in html
    assert "/deploy'" in html or '/deploy"' in html
    assert "onclick=\"deployNet()\"" in html
