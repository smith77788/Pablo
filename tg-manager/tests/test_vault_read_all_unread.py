"""Папка «Непрочитанные» в хранилище: кнопка «Прочитать все» отмечает каждый
непрочитанный чат тем же запросом, что и меню строки, и считает неудачи."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "mini_app/screens/vault_dialogs.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = (
        "let VAULT_FOLDER='unread', VAULT_READ_ONLY=false, _VAULT_CHATS=[];"
        "const calls=[], toasts=[]; let reloads=0;"
        "const vaultPath=p=>p; const errRu=(e,f)=>f;"
        "const toast=m=>toasts.push(m);"
        "async function reloadVaultDialogs(){reloads++;}"
        "async function api(p,o){calls.push([p,JSON.parse(o.body)]);"
        " if(p.includes('/7/'))throw new Error('x');}"
        + _fn("vaultReadAllBar") + _fn("vaultReadAll") + body
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_bar_only_in_unread_folder_for_owner():
    r = _run(
        "const ch=[{chat_id:1,unread:2},{chat_id:2,unread:0},{chat_id:3,unread:1}];"
        "const a=vaultReadAllBar(ch); VAULT_FOLDER='all'; const b=vaultReadAllBar(ch);"
        "VAULT_FOLDER='unread'; VAULT_READ_ONLY=true; const c=vaultReadAllBar(ch);"
        "VAULT_READ_ONLY=false; const d=vaultReadAllBar([{chat_id:1,unread:0}]);"
        "console.log(JSON.stringify([a,b,c,d]))"
    )
    assert "Прочитать все (2)" in r[0]
    assert r[1:] == ["", "", ""]


def test_read_all_marks_each_and_reports_failures():
    r = _run(
        "_VAULT_CHATS=[{chat_id:5,unread:1},{chat_id:6,unread:0},{chat_id:7,unread:3}];"
        "vaultReadAll().then(()=>console.log(JSON.stringify({calls,toasts,reloads})))"
    )
    assert r["calls"] == [
        ["/api/miniapp/vault/chat/5/state", {"read": True}],
        ["/api/miniapp/vault/chat/7/state", {"read": True}],
    ]
    assert r["toasts"] == ["👁 Прочитано: 1, не получилось: 1"]
    assert r["reloads"] == 1


def test_list_renders_the_bar():
    assert "vaultReadAllBar(_VAULT_CHATS) + _VAULT_CHATS.map(vaultRowHtml)" in HTML
