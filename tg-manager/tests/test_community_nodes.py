"""Ноды-комьюнити (mini-Discord, модель B): схема, сервис, op, эндпоинты, UI."""
from __future__ import annotations

import asyncio
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _func_src(path: str, name: str) -> str:
    """Тело функции по границам AST.

    Раньше резали окном фиксированной длины (`ow[i:i+2600]`): любая вставка
    внутрь функции выталкивала проверяемое за границу, и тест падал на исправном
    коде. Границы AST от длины тела не зависят.
    """
    import ast
    src = open(path, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {path}")



def test_schema_defines_community_tables():
    sql = open(os.path.join(ROOT, "schema_v177.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS community_nodes" in sql
    assert "CREATE TABLE IF NOT EXISTS community_channels" in sql
    assert "tg_topic_id" in sql
    assert "REFERENCES community_nodes(id) ON DELETE CASCADE" in sql


def test_nodes_engine_has_community_api():
    src = open(os.path.join(ROOT, "services", "nodes_engine.py"), encoding="utf-8").read()
    for fn in ("register_community_node", "list_community_nodes",
               "list_community_channels", "deactivate_community_node",
               "create_community_channel"):
        assert f"async def {fn}" in src
    # канал = форум-топик (Bot API create_forum_topic)
    i = src.index("async def create_community_channel")
    assert "create_forum_topic" in src[i:i + 1200]


class _FakePool:
    def __init__(self, rows=None, row=None):
        self._rows, self._row = rows or [], row

    async def fetch(self, q, *a):
        return self._rows

    async def fetchrow(self, q, *a):
        return self._row

    async def execute(self, q, *a):
        return "UPDATE 1"


def test_register_and_list_are_owner_scoped():
    from services import nodes_engine
    pool = _FakePool(row={"id": 1, "owner_id": 7, "tg_chat_id": -100, "title": "T",
                          "description": "", "is_active": True})
    node = asyncio.run(nodes_engine.register_community_node(pool, 7, -100, "T"))
    assert node["id"] == 1 and node["owner_id"] == 7
    pool2 = _FakePool(rows=[{"id": 1, "owner_id": 7, "channels": 3}])
    lst = asyncio.run(nodes_engine.list_community_nodes(pool2, 7))
    assert lst[0]["channels"] == 3


def test_add_channel_goes_through_op():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    assert 'op_type == "community_add_channel"' in ow
    assert "async def _exec_community_add_channel" in ow
    i = ow.index("async def _exec_community_add_channel")
    assert "create_community_channel" in ow[i:i + 900]
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    i2 = api.index("async def community_channel_add")
    assert "operation_bus.submit" in api[i2:i2 + 1400]


def test_endpoints_and_routes():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    for r in ('"/api/miniapp/community/nodes"', '"/api/miniapp/community/node"',
              '"/api/miniapp/community/node/{node_id}/channels"'):
        assert r in api
    for fn in ("community_nodes_list", "community_node_create", "community_channel_add"):
        assert f"async def {fn}" in api


def test_frontend_community_ui():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert 'id="s-community"' in html and 'id="s-communitynode"' in html
    assert "function openCommunity" in html
    assert "function addCommunityChannel" in html
    assert "/api/miniapp/community/nodes" in html
    assert 'onclick="openCommunity()"' in html


def test_community_organism_and_invite_wiring():
    world = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "community_nodes" in world and "community_empty" in world
    brain = open(os.path.join(ROOT, "services", "organism", "brain.py"), encoding="utf-8").read()
    assert "community_empty" in brain and '"kind": "community"' in brain
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    # инвайт в ноду подставляет её группу
    assert "function inviteToCommunity" in html and "massInviteGroup" in html
    # действие мозга community маршрутизируется
    assert "if (k==='community') return openCommunity();" in html


def test_node_type_in_deploy_plan_and_executor():
    nb = open(os.path.join(ROOT, "services", "network_builder.py"), encoding="utf-8").read()
    assert '"node":' in nb and '"community":' in nb and '"factory": "community"' in nb
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = ow.index("async def _exec_deploy_network")
    fn = ow[i:i + 9000]
    assert 'ntype in ("node", "community")' in fn
    assert "create_forum_supergroup" in fn and "register_community_node" in fn


def test_forum_supergroup_helper():
    am = open(os.path.join(ROOT, "services", "account_manager.py"), encoding="utf-8").read()
    assert "async def create_forum_supergroup" in am
    i = am.index("async def create_forum_supergroup")
    assert "ToggleForumRequest" in am[i:i + 1200] and "megagroup=True" in am[i:i + 1200]


def test_liven_and_staff_ops_and_schema():
    sql = open(os.path.join(ROOT, "schema_v178.sql"), encoding="utf-8").read()
    assert "community_node_members" in sql and "role" in sql
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    for op in ('op_type == "community_liven"', 'op_type == "community_set_staff"'):
        assert op in ow
    assert "async def _exec_community_liven" in ow
    assert "async def _exec_community_set_staff" in ow
    # оживление = вступление флота по инвайт-ссылке (работает и для приватных
    # нод) + запись участником; роли = промоут владельцем/админом ноды
    _owp = os.path.join(ROOT, "services", "op_worker.py")
    liven = _func_src(_owp, "_exec_community_liven")
    assert "create_chat_invite_link" in liven and "join_channel(" in liven
    assert "add_node_member" in liven
    staff = _func_src(_owp, "_exec_community_set_staff")
    assert "promote_to_admin" in staff
    assert "role='admin'" in staff   # промоутер — админ-член ноды (владелец)


def test_members_endpoints_and_ui():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    for r in ('"/api/miniapp/community/node/{node_id}/members"',
              '"/api/miniapp/community/node/{node_id}/liven"',
              '"/api/miniapp/community/node/{node_id}/staff"'):
        assert r in api
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function livenCommunity" in html and "function setCommunityStaff" in html
    assert 'id="s-communitymembers"' in html
