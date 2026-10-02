"""«Создать сеть по шаблону» шаблона не применяла.

В экране Network Builder есть выбор шаблона (хаб-спицы, цепочка, меш, дерево) и
отдельное действие «Создать сеть по шаблону» — с подтверждением «Создать сеть по
шаблону "hub_spoke"?» и подписью «✅ Сеть по шаблону создана». Клиент посылал
`{name, template, description}`, а обработчик читал ОДНО поле — `name`:

    name = validate_string(body.get("name"), max_len=100)
    ...
    "INSERT INTO network_instances(owner_id, name, status) VALUES($1,$2,'active')"

То есть человек выбирал топологию, подтверждал, видел «создано» и получал пустую
сеть. Описание он тоже писал в пустоту: колонки под него не было вовсе.

Здесь стережётся: шаблон превращается в узлы и рёбра, рёбра ссылаются на
созданные узлы (иначе связку нельзя развернуть), неизвестный шаблон — отказ, а
не тихо пустая сеть, и описание доезжает до базы и обратно в список.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api as M
from services import network_builder as NB

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
UID = 414141
NET_ID = 77


class _Pool:
    """Пул, который помнит вставки и выдаёт узлам разные id."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self._next_id = 100

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return []

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        self._next_id += 1
        return {"id": self._next_id}

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return NET_ID

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


class _Req:
    def __init__(self, body: dict):
        self._body = body
        self.rel_url = type("U", (), {"query": {}})()
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.query_string = ""
        self.method = "POST"
        self.match_info: dict[str, str] = {}

    async def json(self):
        return self._body


def _handler(pool, method: str, path: str):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        p = info.get("path") or info.get("formatter") or ""
        if route.method == method and p == path:
            return route.handler
    raise AssertionError(f"роут {method} {path} не зарегистрирован")


def _create(pool, body: dict):
    h = _handler(pool, "POST", "/api/miniapp/networks")
    return asyncio.run(h(_Req(body)))


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


# ── Шаблон как данные ────────────────────────────────────────────────────────

def test_every_offered_template_exists():
    """Экран предлагает четыре шаблона — все четыре должны быть описаны."""
    offered = set(re.findall(r'<option value="(\w+)">[^<]*</option>',
                             INDEX.read_text("utf-8")))
    for key in ("hub_spoke", "chain", "mesh", "tree"):
        assert key in offered, f"{key} исчез из выбора на экране"
        assert NB.topology_template(key), f"шаблон {key} не описан на сервере"


def test_an_unknown_template_is_not_silently_empty():
    assert NB.topology_template("нет-такого") is None
    assert NB.topology_template("") is None


@pytest.mark.parametrize("key", ["hub_spoke", "chain", "mesh", "tree"])
def test_a_template_is_deployable(key):
    """Узел, который нечем создать, и ребро в пустоту делают связку
    неразворачиваемой — `plan_deployment.ready` станет False."""
    tpl = NB.topology_template(key)
    n = len(tpl["nodes"])
    assert n >= 2, f"в шаблоне {key} меньше двух узлов — топологии нет"
    assert tpl["edges"], f"в шаблоне {key} нет рёбер"
    for node in tpl["nodes"]:
        assert node["type"] in NB._NODE_FACTORY, (
            f"узел типа {node['type']} не умеет создавать ни одна фабрика")
        assert node["label"], "узел без подписи — в графе будет «#»"
    for i, j, etype in tpl["edges"]:
        assert 0 <= i < n and 0 <= j < n, f"ребро {i}→{j} вне узлов шаблона"
        assert i != j, "ребро узла в себя"
        assert etype in NB._EDGE_ACTION, f"ребро {etype} не описано ни одним действием"
    # План развёртывания должен считать такую связку готовой.
    nodes = [{"id": k, "type": t["type"], "label": t["label"], "object_id": None}
             for k, t in enumerate(tpl["nodes"])]
    edges = [{"from_id": i, "to_id": j, "type": e} for i, j, e in tpl["edges"]]
    plan = NB.plan_deployment(nodes, edges)
    assert plan["ready"] is True, f"связку по шаблону {key} нельзя развернуть: {plan}"
    assert plan["create"] == n and plan["wire"] == len(tpl["edges"])


def test_template_edges_bind_the_created_nodes_not_their_numbers():
    """Ребро по НОМЕРУ узла в шаблоне, а не по id из базы, связало бы чужие узлы."""
    pool = _Pool()
    res = asyncio.run(NB.apply_template(pool, NET_ID, "chain"))
    assert res["ok"] and res["nodes"] == 4 and res["edges"] == 3, res
    # Пул выдаёт вставленным узлам id подряд, начиная с 101.
    node_count = sum(1 for q, _ in pool.calls if "INSERT INTO network_nodes" in q)
    node_ids = list(range(101, 101 + node_count))
    edge_args = [a for q, a in pool.calls if "INSERT INTO network_edges" in q]
    assert len(edge_args) == 3
    for args in edge_args:
        src, dst = args[1], args[2]
        assert src in node_ids and dst in node_ids, (
            f"ребро {src}→{dst} ссылается не на созданные узлы {node_ids}")
        assert src != dst


# ── Через эндпоинт ───────────────────────────────────────────────────────────

def test_creating_with_a_template_creates_the_topology():
    pool = _Pool()
    d = _body(_create(pool, {"name": "Моя сеть", "template": "hub_spoke"}))
    assert d["ok"] is True
    assert d["nodes"] == 5 and d["edges"] == 4, (
        f"шаблон не применён: {d} — человеку обещали сеть по шаблону")
    assert sum(1 for q, _ in pool.calls if "INSERT INTO network_nodes" in q) == 5


def test_creating_without_a_template_stays_empty():
    """«Пустая сеть» — тоже выбор на экране, его ломать нельзя."""
    pool = _Pool()
    d = _body(_create(pool, {"name": "Пустая"}))
    assert d["ok"] is True and d["nodes"] == 0
    assert not any("INSERT INTO network_nodes" in q for q, _ in pool.calls)


def test_an_unknown_template_is_refused():
    pool = _Pool()
    r = _create(pool, {"name": "Сеть", "template": "хакерский"})
    assert r.status == 400, "неизвестный шаблон молча дал пустую сеть"
    assert not any("INSERT INTO network_instances" in q for q, _ in pool.calls), (
        "сеть создана до проверки шаблона — осталась пустая сеть с именем")


def test_the_description_reaches_the_database():
    pool = _Pool()
    _create(pool, {"name": "Сеть", "description": "для клиентских каналов"})
    inserts = [(q, a) for q, a in pool.calls if "INSERT INTO network_instances" in q]
    assert inserts, "сеть не создана"
    q, args = inserts[0]
    assert "description" in q, "описание снова некуда писать"
    assert "для клиентских каналов" in args, "описание не доехало параметром"


def test_the_list_returns_the_description_so_the_screen_can_show_it():
    """Записать и не показать — то же, что потерять."""
    src = Path(M.__file__).read_text("utf-8")
    i = src.index("async def networks_list")
    block = src[i:src.index("async def network_create_plural")]
    assert "description" in block, "список сетей не отдаёт описание"
    assert "n.description" in INDEX.read_text("utf-8"), (
        "карточка сети не показывает описание")


def test_a_planned_node_does_not_print_id_null():
    """Узел шаблона ещё не привязан к объекту: «ID: null» читается как поломка."""
    html = INDEX.read_text("utf-8")
    i = html.index("txt('netNodesList'")
    block = html[i:i + 700]
    assert "ID: ' + n.object_id" in block or "n.object_id ?" in block, (
        "подпись узла снова печатает object_id без проверки")
    assert "· ID: ${n.object_id}" not in block
