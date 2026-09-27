"""Связки Фаза 2: исполнитель развёртывания (op deploy_network) — wiring/контракт."""
from __future__ import annotations

import ast
import os
from services import op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ow():
    return open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()


def _func_src(path: str, name: str) -> str:
    """Текст функции по границам из ast, а не окном фиксированной длины.

    Окно «заголовок плюс N символов» промахивается, стоит функции подрасти
    на пару строк: проверка ничего не находит и молча перестаёт защищать.
    """
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def _api():
    return open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def test_op_dispatch_wired():
    src = _ow()
    assert op_worker.handler_for("deploy_network") is not None
    assert "async def _exec_deploy_network" in src


def test_executor_creates_persists_and_wires():
    fn = _func_src(os.path.join(ROOT, "services", "op_worker.py"),
                   "_exec_deploy_network")
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


def test_executor_handles_attach_discussion_group():
    fn = _func_src(os.path.join(ROOT, "services", "op_worker.py"),
                   "_exec_deploy_network")
    # attach/link рёбра прикрепляют группу как чат обсуждений
    assert "set_discussion_group" in fn
    assert '"attach", "link"' in fn
    # crosspost — ручной шаг (нативного API нет)
    assert '== "crosspost"' in fn


def test_set_discussion_group_helper_exists():
    am = open(os.path.join(ROOT, "services", "account_manager.py"), encoding="utf-8").read()
    assert "async def set_discussion_group" in am
    assert "SetDiscussionGroupRequest" in am


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
