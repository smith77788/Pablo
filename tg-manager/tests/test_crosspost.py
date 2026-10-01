"""Связки 2c: кросспостинг — правила, forward-хелпер, op, эндпоинты, UI."""
from __future__ import annotations

import os
from services import op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _func_src(path: str, name: str) -> str:
    """Тело функции по границам AST.

    Раньше здесь резали окном фиксированной длины (`src[i:i+3200]`). Любая
    вставка внутрь функции выталкивала проверяемое за границу окна, и тест падал
    на исправном коде — мерил не то. Границы AST от длины тела не зависят.
    """
    import ast
    src = open(path, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {path}")



def test_schema_defines_crosspost_links():
    sql = open(os.path.join(ROOT, "schema_v176.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS crosspost_links" in sql
    assert "last_msg_id" in sql
    assert "UNIQUE (owner_id, source_channel_id, target_channel_id)" in sql


# Окно фиксированной длины (`am[i:i + 2000]`) стояло и здесь, ровно с тем же
# исходом, что описан в docstring `_func_src`: добавление строк внутрь функции
# вытолкнуло проверяемое за границу окна, и два теста упали на исправном коде.
_AM = os.path.join(ROOT, "services", "account_manager.py")


def test_forward_helper_exists():
    am = open(_AM, encoding="utf-8").read()
    assert "async def forward_new_posts" in am
    fn = _func_src(_AM, "forward_new_posts")
    assert "iter_messages" in fn and "forward_messages" in fn
    assert "min_id=since" in fn and "reverse=True" in fn


def test_forward_new_link_seeds_cursor_no_backlog_dump():
    # Новая связка (курсор=0) НЕ форвардит старый бэклог — только ставит курсор.
    fn = _func_src(_AM, "forward_new_posts")
    assert "if since <= 0:" in fn
    assert '"seeded": True' in fn


def test_deploy_creates_crosspost_rule():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    fn = _func_src(os.path.join(ROOT, "services", "op_worker.py"), "_exec_deploy_network")
    assert 'etype == "crosspost"' in fn
    assert "INSERT INTO crosspost_links" in fn


def test_crosspost_run_op_and_dispatch():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    assert op_worker.handler_for("crosspost_run") is not None
    assert "async def _exec_crosspost_run" in ow
    fn = _func_src(os.path.join(ROOT, "services", "op_worker.py"), "_exec_crosspost_run")
    assert "forward_new_posts" in fn
    # Курсор двигается, и двигается ПО ХОДУ работы: одной записи в конце
    # недостаточно — перезапуск посередине пачки пересылал посты второй раз.
    assert "on_forwarded=" in fn
    assert "UPDATE crosspost_links" in fn and "last_msg_id" in fn
    assert "_governed_sleep" in fn and "_is_cancelled" in fn


def test_endpoints_through_bus_and_routes():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def crosspost_run" in api
    assert "async def crosspost_links_list" in api
    assert '"/api/miniapp/crosspost/run"' in api
    assert '"/api/miniapp/crosspost/links"' in api
    # Границы функции из ast, а не окно в 900 символов: окно промахивалось,
    # стоило добавить в функцию пару строк, и проверка «через шину» замолкала.
    fn = _func_src(os.path.join(ROOT, "services", "mini_app_api.py"), "crosspost_run")
    assert 'operation_bus.submit' in fn


def test_frontend_crosspost_ui():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function loadCrosspost" in html
    assert "function runCrosspost" in html
    assert "/api/miniapp/crosspost/links" in html
    assert 'onclick="runCrosspost()"' in html
