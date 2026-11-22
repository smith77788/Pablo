"""Автопостинг канала: окно помнит ритм прошлого запуска, хранит черновик
текста (случайно закрытое окно не стирает набранное) и показывает счётчик
символов до лимита Telegram."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/channel_manage.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


STUBS = (
    "const els={};function el(id){return els[id]||(els[id]={id,value:'',textContent:'',style:{},"
    " selectedIndex:0,options:[{textContent:'x'}]});}"
    "const document={getElementById:el};"
    "const drafts={},prefs={},log=[];"
    "const saveDraft=(k,v)=>{drafts[k]=v;};const loadDraft=k=>drafts[k]||'';"
    "const clearDraft=k=>{delete drafts[k];};"
    "const saveFormPrefs=(k,o)=>{prefs[k]=Object.assign(prefs[k]||{},o);};"
    "const applyFormPrefs=(k,m)=>log.push(['apply',k,m]);"
    "const _cmIdsNorm=x=>x;const _cmWhat=()=>'канал';const esc=s=>s;"
    "const _cmModal=()=>{};const showModal=()=>{};const loadChAutoposts=()=>{};"
    "const askConfirm=async()=>true;const toast=()=>{};const closeModal=()=>{};"
    "const _cmAfterBulk=()=>{};const errRu=(e,f)=>f;let _cmIds=[];"
    "async function api(p,o){log.push(['api',p,JSON.parse(o.body)]);return {};}"
)


def _run(body):
    js = (STUBS + "const _CAP_MAX = 4096;" + _fn("_capOnInput") + _fn("_cmSelLabel")
          + _fn("openChAutopost") + _fn("submitChAutopost") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_open_restores_rhythm_and_draft():
    r = _run(
        "drafts.ch_autopost='Черновик';openChAutopost([5]);"
        "console.log(JSON.stringify({t:el('capText').value,len:el('capLen').textContent,log}))"
    )
    assert r["t"] == "Черновик"
    assert r["len"] == "8 / 4096"
    assert ["apply", "ch_autopost", {"every": "capEvery", "start": "capStart", "count": "capCount"}] in r["log"]


def test_counter_turns_red_over_limit():
    r = _run(
        "el('capText').value='a'.repeat(4097);_capOnInput();"
        "console.log(JSON.stringify([el('capLen').textContent,el('capLen').style.color]))"
    )
    assert r == ["4097 / 4096", "var(--red)"]


def test_successful_launch_remembers_rhythm_and_drops_draft():
    r = _run(
        "_cmIds=[5];el('capText').value='Пост';_capOnInput();"
        "el('capEvery').value='180';el('capStart').value='0';el('capCount').value='7';"
        "submitChAutopost().then(()=>console.log(JSON.stringify({drafts,prefs})))"
    )
    assert r["drafts"] == {}
    assert r["prefs"]["ch_autopost"] == {"every": "180", "start": "0", "count": "7"}
