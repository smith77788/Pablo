"""Связки: чистый план развёртывания топологии + wiring эндпоинта/UI."""
from __future__ import annotations

import os

from services import network_builder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_plan_creates_missing_nodes_then_wires():
    nodes = [
        {"id": 1, "type": "channel", "label": "Новости", "object_id": None},
        {"id": 2, "type": "bot", "label": "Админ-бот", "object_id": None},
        {"id": 3, "type": "group", "label": "Чат", "object_id": 555},  # уже существует
    ]
    edges = [
        {"from_id": 2, "to_id": 1, "type": "admin", "from_label": "Админ-бот", "to_label": "Новости"},
        {"from_id": 3, "to_id": 1, "type": "attach", "from_label": "Чат", "to_label": "Новости"},
    ]
    p = network_builder.plan_deployment(nodes, edges)
    assert p["create"] == 2          # канал+бот (группа существует)
    assert p["wire"] == 2
    # шаги: сперва create, потом wire
    kinds = [s["kind"] for s in p["steps"]]
    assert kinds == ["create", "create", "wire", "wire"]
    assert p["factories"] == {"channel": 1, "bot": 1}
    assert p["ready"] is True


def test_plan_all_existing_no_creates():
    nodes = [{"id": 1, "type": "channel", "label": "C", "object_id": 10}]
    edges = []
    p = network_builder.plan_deployment(nodes, edges)
    assert p["create"] == 0 and p["wire"] == 0
    assert p["ready"] is False        # нечего разворачивать


def test_plan_dangling_edge_not_ready():
    nodes = [{"id": 1, "type": "bot", "label": "B", "object_id": None}]
    edges = [{"from_id": 1, "to_id": 999, "type": "admin"}]  # 999 не существует
    p = network_builder.plan_deployment(nodes, edges)
    assert p["ready"] is False


def test_edge_action_text_uses_labels():
    nodes = [{"id": 1, "type": "bot", "label": "Бот", "object_id": 1},
             {"id": 2, "type": "channel", "label": "Канал", "object_id": 2}]
    edges = [{"from_id": 1, "to_id": 2, "type": "admin", "from_label": "Бот", "to_label": "Канал"}]
    p = network_builder.plan_deployment(nodes, edges)
    wire = [s for s in p["steps"] if s["kind"] == "wire"][0]
    assert "Бот" in wire["text"] and "Канал" in wire["text"]


def test_endpoint_and_ui_wired():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def network_deploy_plan" in api
    assert '"/api/miniapp/networks/{net_id}/deploy_plan"' in api
    assert "plan_deployment" in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function showNetDeployPlan" in html
    assert "/deploy_plan" in html
