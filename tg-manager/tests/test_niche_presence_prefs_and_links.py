"""Радар ниши: настройки присутствия (что делать, аккаунтов, действий)
запоминаются с прошлого запуска, а ссылки показанной части каталога
копируются одним списком без повторов и мусора."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/niche.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = (
        "const prefs={};const saveFormPrefs=(k,o)=>{prefs[k]=JSON.parse(JSON.stringify(o));};"
        "const loadFormPrefs=k=>prefs[k]||{};"
        "const acts=[{value:'join',checked:true},{value:'comment',checked:true},{value:'reaction',checked:false}];"
        "const els={nrLimit:{value:'12'},nrAccCount:{value:'3'}};"
        "const document={querySelectorAll:()=>acts,getElementById:id=>els[id]};"
        + _fn("nrLinksText") + _fn("_nrSavePresencePrefs") + _fn("_nrApplyPresencePrefs") + body
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_links_are_unique_and_valid():
    r = _run(
        "console.log(JSON.stringify(nrLinksText([{username:'chat_one'},{username:'@Chat_One'},"
        "{username:''},{username:'bad name'},{username:'news24'}])))"
    )
    assert r == "https://t.me/chat_one\nhttps://t.me/news24"


def test_presence_settings_round_trip():
    r = _run(
        "_nrApplyPresencePrefs();const before=acts.map(a=>a.checked);"
        "_nrSavePresencePrefs(['reaction'],30,5);_nrApplyPresencePrefs();"
        "console.log(JSON.stringify({before,after:acts.map(a=>a.checked),lim:els.nrLimit.value,acc:els.nrAccCount.value}))"
    )
    assert r["before"] == [True, True, False]  # без сохранённого — значения разметки
    assert r["after"] == [False, False, True]
    assert (r["lim"], r["acc"]) == ("30", "5")


def test_wired_into_catalog_and_launch():
    assert "_nrCopyBtn() + items.map(_nrAssetRow)" in SRC
    assert "NR.assetItems = (NR.assetItems || []).concat(items);" in SRC
    launch = _fn("nrPresence")
    assert launch.index("_nrSavePresencePrefs(") > launch.index("askConfirm(")
