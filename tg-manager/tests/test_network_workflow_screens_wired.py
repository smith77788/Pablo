"""Регрессия: разделы Network Builder и Workflows больше не 404/500.

Разрыв контракта: фронт звал plural REST (/networks, /networks/{id}/nodes,
/workflows/{id}, /workflows/{id}/steps), бэк регистрировал singular (/network,
/workflow/{id}) → все data-вызовы падали. Плюс workflow_list звал несуществующий
list_workflows → 500. Фикс: plural-эндпоинты поверх существующих движков/таблиц.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_network_plural_routes_registered():
    api = _read("services/mini_app_api.py")
    for line in [
        'add_get("/api/miniapp/networks", networks_list)',
        'add_post("/api/miniapp/networks", network_create_plural)',
        'add_get("/api/miniapp/networks/{net_id}", network_detail_plural)',
        'add_post("/api/miniapp/networks/{net_id}/nodes", network_add_node)',
        'add_post("/api/miniapp/networks/{net_id}/edges", network_add_edge)',
        'add_delete("/api/miniapp/networks/nodes/{node_id}", network_delete_node)',
        'add_delete("/api/miniapp/networks/edges/{edge_id}", network_delete_edge)',
    ]:
        assert line in api, f"нет маршрута: {line}"
    # detail отдаёт форму графа (from_id/to_id/labels)
    seg = api[api.index("async def network_detail_plural"):api.index("async def network_add_node")]
    assert '"from_id"' in seg and '"to_id"' in seg and '"from_label"' in seg
    # delete узла/ребра owner-scoped (через instance владельца)
    dseg = api[api.index("async def network_delete_node"):api.index("async def network_add_edge")]
    assert "WHERE owner_id=$2" in dseg


def test_workflow_plural_routes_and_list_fixed():
    api = _read("services/mini_app_api.py")
    for line in [
        'add_post("/api/miniapp/workflows", workflow_create_plural)',
        'add_get("/api/miniapp/workflows/{wf_id}", workflow_detail_plural)',
        'add_patch("/api/miniapp/workflows/{wf_id}", workflow_toggle_plural)',
        'add_delete("/api/miniapp/workflows/{wf_id}", workflow_delete_plural)',
        'add_post("/api/miniapp/workflows/{wf_id}/steps", workflow_add_step)',
    ]:
        assert line in api, f"нет маршрута: {line}"
    # workflow_list больше НЕ зовёт несуществующий list_workflows (был 500)
    lst = api[api.index("async def workflow_list"):api.index("async def workflow_execute")]
    assert "import list_workflows" not in lst and "await list_workflows" not in lst
    assert "jsonb_array_length" in lst and "'active'" in lst
    # add_step аппендит в inline jsonb steps
    st = api[api.index("async def workflow_add_step"):api.index("async def operation_export")]
    assert "steps = COALESCE(steps,'[]'::jsonb) || $1::jsonb" in st
