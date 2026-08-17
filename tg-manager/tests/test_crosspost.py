"""Связки 2c: кросспостинг — правила, forward-хелпер, op, эндпоинты, UI."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_schema_defines_crosspost_links():
    sql = open(os.path.join(ROOT, "schema_v176.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS crosspost_links" in sql
    assert "last_msg_id" in sql
    assert "UNIQUE (owner_id, source_channel_id, target_channel_id)" in sql


def test_forward_helper_exists():
    am = open(os.path.join(ROOT, "services", "account_manager.py"), encoding="utf-8").read()
    assert "async def forward_new_posts" in am
    i = am.index("async def forward_new_posts")
    fn = am[i:i + 1600]
    assert "iter_messages" in fn and "forward_messages" in fn
    assert "min_id=int(since_msg_id" in fn and "reverse=True" in fn


def test_deploy_creates_crosspost_rule():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = ow.index("async def _exec_deploy_network")
    fn = ow[i:i + 7000]
    assert 'etype == "crosspost"' in fn
    assert "INSERT INTO crosspost_links" in fn


def test_crosspost_run_op_and_dispatch():
    ow = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    assert 'op_type == "crosspost_run"' in ow
    assert "async def _exec_crosspost_run" in ow
    i = ow.index("async def _exec_crosspost_run")
    fn = ow[i:i + 3200]
    assert "forward_new_posts" in fn
    assert "UPDATE crosspost_links SET last_msg_id" in fn      # курсор двигается
    assert "_governed_sleep" in fn and "_is_cancelled" in fn


def test_endpoints_through_bus_and_routes():
    api = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    assert "async def crosspost_run" in api
    assert "async def crosspost_links_list" in api
    assert '"/api/miniapp/crosspost/run"' in api
    assert '"/api/miniapp/crosspost/links"' in api
    i = api.index("async def crosspost_run")
    assert 'operation_bus.submit' in api[i:i + 900]


def test_frontend_crosspost_ui():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "function loadCrosspost" in html
    assert "function runCrosspost" in html
    assert "/api/miniapp/crosspost/links" in html
    assert 'onclick="runCrosspost()"' in html
