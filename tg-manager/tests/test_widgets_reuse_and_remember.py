"""Виджеты в чатах: «🔁 Ещё раз» подставляет тексты отправленного виджета
в форму нового (не правку старого), а бот и место отправки берутся
с прошлой удачной отправки."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/widgets.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = (
        "const log=[];const WG={tpls:[{id:'poll',fields:[]},{id:'rating',fields:[]}],items:[],edit:{id:1}};"
        "const prefs={widgets:{where:'chat',chat:'@grp'}};const loadFormPrefs=k=>prefs[k]||{};"
        "const els={wgWhere:{value:'me'},wgChat:{value:''},'s-widgets':{querySelector:()=>({scrollTo(){}})}};"
        "const document={getElementById:id=>els[id]};"
        "function wgWhereToggle(){log.push('toggle');}function _wgModeLabels(){log.push('labels:'+!!WG.edit);}"
        "function wgPickTpl(t){log.push('pick:'+t);}function _wgTpl(id){return WG.tpls.find(t=>t.id===id);}"
        "function _wgRenderFields(t,d){log.push('fields:'+t.id+':'+JSON.stringify(d));}"
        "function wgPreview(){log.push('preview');}const toast=()=>{};"
        + _fn("_wgApplyWhere") + _fn("wgCanReuse") + _fn("wgReuse") + body
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_reuse_fills_a_new_widget_from_the_old_one():
    r = _run(
        "WG.items=[{id:7,template:'poll',data:{q:'Кофе?',o:['Да','Нет']}},{id:8,template:'html'}];"
        "const can=[wgCanReuse(WG.items[0]),wgCanReuse(WG.items[1]),wgCanReuse({template:'gone'})];"
        "wgReuse(7);console.log(JSON.stringify({can,edit:WG.edit,log}))"
    )
    assert r["can"] == [True, False, False]
    assert r["edit"] is None
    assert r["log"] == ["labels:false", "pick:poll", 'fields:poll:{"q":"Кофе?","o":["Да","Нет"]}', "preview"]


def test_where_and_group_come_from_last_send():
    r = _run("_wgApplyWhere();console.log(JSON.stringify([els.wgWhere.value,els.wgChat.value,log]))")
    assert r == ["chat", "@grp", ["toggle"]]


def test_wired_into_send_list_and_bots():
    send = _fn("wgSend")
    assert send.index("saveFormPrefs('widgets'") > send.index("api('/api/miniapp/widgets/send'")
    assert "wgCanReuse(w) ?" in _fn("_wgRow")
    assert "loadFormPrefs('widgets').bot" in _fn("_wgLoadBots")
