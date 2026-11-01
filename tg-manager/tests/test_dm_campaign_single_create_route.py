"""Кампанию личных сообщений в мини-аппе создаёт ОДНА ручка.

ЧТО БЫЛО. Две: `POST /api/miniapp/dm_campaigns` (`mini_app_api.
dm_campaign_create` — сегменты, когорты, импорт-список, восемь видов целей) и
`POST /api/miniapp/dm_campaign` (`mini_app_dm.create_dm_campaign` — своя
проверка текста и половина целей: `crm` и `parsed_audience` принимались, но
получателей не считались, и кампания ложилась с нулём). Экран звал первую,
вторая была жива и ждала, пока очередной агент выберет её по имени — вывод
из CLAUDE.md: мёртвый дубль опасен тем, что выглядит удобнее живого.

Мерка — по смыслу, а не по имени: ищем в слое мини-аппа КАЖДЫЙ обработчик,
который пишет `INSERT INTO dm_campaigns`, и КАЖДЫЙ маршрут, который на него
смотрит. Переименование функции или переезд в новый `mini_app_*.py` её не
обманут.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_LAYER = sorted((ROOT / "services").glob("mini_app*.py"))
_ONLY_ROUTE = "/api/miniapp/dm_campaigns"


def _creating_routes(src: str) -> list[str]:
    tree = ast.parse(src)
    creators = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "INSERT INTO dm_campaigns" in (ast.get_source_segment(src, n) or "")
    }
    routes = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "add_post" and len(n.args) >= 2
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[1], ast.Name)
                and n.args[1].id in creators):
            routes.append(n.args[0].value)
    return routes


def test_layer_is_found():
    # Пустой glob сделал бы проверку ниже зелёной ни о чём.
    assert any(p.name == "mini_app_api.py" for p in _LAYER)
    assert any(p.name == "mini_app_dm.py" for p in _LAYER)


def test_exactly_one_route_creates_dm_campaign():
    found = []
    for p in _LAYER:
        found += [(p.name, r) for r in _creating_routes(p.read_text(encoding="utf-8"))]
    assert [r for _, r in found] == [_ONLY_ROUTE], found


def test_probe_sees_a_second_creator():
    # Мерка обязана видеть дубль — иначе она слепа, а не код чист.
    src = (
        "def setup(app, pool):\n"
        "    async def a(r):\n"
        "        await pool.execute('INSERT INTO dm_campaigns(x) VALUES(1)')\n"
        "    async def b(r):\n"
        "        await pool.execute('INSERT INTO dm_campaigns(x) VALUES(2)')\n"
        "    app.router.add_post('/api/miniapp/dm_campaigns', a)\n"
        "    app.router.add_post('/api/miniapp/dm_campaign', b)\n"
    )
    assert sorted(_creating_routes(src)) == [
        "/api/miniapp/dm_campaign", "/api/miniapp/dm_campaigns"]


def test_screen_creates_through_the_live_route():
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "api('/api/miniapp/dm_campaigns', {\n      method:'POST'" in html
