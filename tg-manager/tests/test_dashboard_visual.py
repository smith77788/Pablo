"""Визуальный дашборд «Командный центр»: бэкенд-эндпоинт + фронтенд-рендер.

Экран s-cmdcenter (mini_app/screens/cmdcenter.js) рисует SVG по данным одного
запроса /api/miniapp/dashboard/visual. Проверяем: эндпоинт+маршрут+owner-скоуп+
trust_score на бэкенде; наличие рендер-функций и SVG-примитивов на фронте.
"""
from __future__ import annotations

import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_endpoint_and_route_registered():
    api = _read("services/mini_app_api.py")
    assert "async def dashboard_visual" in api
    assert 'app.router.add_get("/api/miniapp/dashboard/visual", dashboard_visual)' in api


def test_backend_scoped_and_uses_trust_score():
    api = _read("services/mini_app_api.py")
    seg = api[api.index("async def dashboard_visual"):]
    seg = seg[:seg.index("    # ── Bots")]
    # owner-скоуп (не даём чужие данные) + health по trust_score (каноничный)
    assert "_get_uid(request)" in seg and 'return _err("Unauthorized", 401)' in seg
    assert "owner_id=$1" in seg
    assert "trust_score" in seg and "health_score" not in seg
    # четыре блока метрик реально запрашиваются
    for key in ("acc_by_status", "proxies", "ops7d", "health"):
        assert key in seg, f"нет метрики {key}"


def test_frontend_screen_registered_and_included():
    assert os.path.exists(os.path.join(ROOT, "mini_app/screens/cmdcenter.js"))
    html = _read("mini_app/index.html")
    assert '<script src="screens/cmdcenter.js"></script>' in html
    # nav-плитка ведёт на дашборд
    assert 'onclick="openCmdCenter()"' in html


def test_frontend_render_logic_present():
    js = _read("mini_app/screens/cmdcenter.js")
    for fn in ("function openCmdCenter", "function _ccRender", "function _ccDonut",
               "function _ccBar", "function _ccEnsureScreen"):
        assert fn in js, f"нет {fn}"
    # использует общий api-хелпер и правильный маршрут
    assert "api('/api/miniapp/dashboard/visual')" in js
    # экран создаётся динамически (не трогаем HTML index.html)
    assert "createElement('div')" in js and "'s-cmdcenter'" in js
    # рисует SVG
    assert "<svg" in js and "<circle" in js


def test_frontend_render_binds_data():
    """Если есть node — реально прогоняем _ccRender на sample и проверяем биндинг."""
    node = shutil.which("node")
    if not node:
        return  # окружение без node — пропускаем (бэкенд/структуру уже проверили)
    harness = r'''
global.esc = s => String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
global.push=()=>{};global.api=()=>{};global.toast=()=>{};global.back=()=>{};global.STACK=[];
const code=require('fs').readFileSync(process.argv[1],'utf8');
eval(code.replace(/^.use strict.;?$/m,''));
const s={accounts:[{status:'active',count:12},{status:'banned',count:2},{status:'cooldown',count:3}],
proxies:{total:20,alive:15,dead:5,backup:4,assigned:14},
ops7d:[{day:'07-09',done:22,failed:3}],ops_now:{running:2,pending:7},health:{avg:64,good:9,warn:5,bad:3}};
const h=_ccRender(s);
const ok = h.includes('<svg') && h.includes('>17<') && h.includes('>15<')
        && h.includes('>64<') && h.includes('07-09');
process.stdout.write(ok ? 'PASS' : 'FAIL:'+h.slice(0,200));
'''
    js_path = os.path.join(ROOT, "mini_app/screens/cmdcenter.js")
    out = subprocess.run([node, "-e", harness, js_path],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "PASS", f"render mismatch: {out.stdout} {out.stderr}"
