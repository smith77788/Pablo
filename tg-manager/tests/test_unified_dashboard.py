"""Единый дашборд (mini_app/screens/dashboard.js): один экран-хаб со вкладками,
объединяющий инфраструктуру (командный центр), аналитику и все детальные
дашборды. Проверяем структуру/переиспользование/рендер вкладок на живых данных.
"""
from __future__ import annotations

import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_files_present_and_wired():
    assert os.path.exists(os.path.join(ROOT, "mini_app/screens/dashboard.js"))
    html = _read("mini_app/index.html")
    # подключён после cmdcenter.js (переиспользует _ccRender)
    assert '<script src="screens/dashboard.js"></script>' in html
    i_cc = html.index("screens/cmdcenter.js")
    i_db = html.index("screens/dashboard.js")
    assert i_cc < i_db, "dashboard.js должен грузиться после cmdcenter.js"
    # единая точка входа заменила отдельную плитку командного центра
    assert 'onclick="openUnifiedDashboard()"' in html


def test_hub_structure():
    js = _read("mini_app/screens/dashboard.js")
    for fn in ("function openUnifiedDashboard", "function _udTab", "function _udLoad",
               "function _udRenderAnalytics", "function _udRenderMore", "function _udEnsureScreen"):
        assert fn in js, f"нет {fn}"
    # три вкладки
    for key in ("'infra'", "'analytics'", "'more'"):
        assert key in js
    # тянет реальные эндпоинты
    assert "api('/api/miniapp/dashboard/visual')" in js
    assert "/api/miniapp/dashboard_realtime" in js
    # переиспользует рендер командного центра, не дублирует
    assert "_ccRender" in js
    # запуск детальных экранов из хаба (ничего не потеряно). Второй «Дашборд
    # метрик» (openAnalyticsDashboard) удалён по правилу «один дашборд».
    for fn in ("openHealth", "openBotStats", "openAdminStats"):
        assert fn in js, f"нет запуска {fn}"
    # динамический экран (не трогаем разметку index.html)
    assert "createElement('div')" in js and "'s-uni-dash'" in js


def test_render_binds_live_data():
    """Если есть node — прогоняем рендер вкладок на sample и проверяем биндинг."""
    node = shutil.which("node")
    if not node:
        return
    harness = r'''
global.esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
global.num=n=>Number(n).toLocaleString('ru');
global.push=()=>{};global.api=()=>{};global.back=()=>{};global.window={};
const rd=p=>require('fs').readFileSync(p,'utf8').replace(/^.use strict.;?$/m,'');
eval(rd(process.argv[1])); eval(rd(process.argv[2]));
// аналитика
const a=_udRenderAnalytics({total_subscribers:15230,total_channels:12,growth_7d:1240,
  subs_history:[{value:1},{value:2},{value:3}],top_channels:[{name:'Канал А',subscribers:8000}],
  recent_activity:[{text:'событие'}]});
const aOk = /15.230/.test(a) && /\+1.240/.test(a) && a.includes('<polyline')
         && a.includes('Канал А') && a.includes('событие');
// инфра через переиспользуемый _ccRender
const inf=_ccRender({accounts:[{status:'active',count:12},{status:'banned',count:2}],
  proxies:{total:10,alive:8,dead:2,backup:2,assigned:7},ops7d:[{day:'07-15',done:20,failed:2}],
  ops_now:{running:1,pending:4},health:{avg:70,good:9,warn:3,bad:2}});
const iOk = inf.includes('<svg') && inf.includes('>14<');
process.stdout.write(aOk && iOk ? 'PASS' : 'FAIL a='+aOk+' i='+iOk);
'''
    cc = os.path.join(ROOT, "mini_app/screens/cmdcenter.js")
    db = os.path.join(ROOT, "mini_app/screens/dashboard.js")
    out = subprocess.run([node, "-e", harness, cc, db],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "PASS", f"render mismatch: {out.stdout} {out.stderr}"
