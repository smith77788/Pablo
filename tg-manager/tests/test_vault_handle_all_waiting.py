"""Хранилище, папка «Ждут ответа»: «✅ Отметить все отвеченными» —
с подтверждением, тем же запросом {handled:true}, что меню строки."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/vault_dialogs.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body, confirm="true"):
    js = ("let VAULT_FOLDER='waiting',VAULT_READ_ONLY=false,_VAULT_CHATS=[];const calls=[],toasts=[];let reloads=0;"
          "const vaultPath=p=>p;const errRu=(e,f)=>f;const toast=m=>toasts.push(m);const plural=(n,a,b,c)=>c;"
          "const askConfirm=async()=>" + confirm + ";async function reloadVaultDialogs(){reloads++;}"
          "async function api(p,o){calls.push([p,JSON.parse(o.body)]);}"
          + _fn("vaultReadAllBar") + _fn("vaultHandleAll") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_bar_in_waiting_folder():
    r = _run("const ch=[{chat_id:1,awaiting_reply:true},{chat_id:2},{chat_id:3,awaiting_reply:true}];"
             "const a=vaultReadAllBar(ch);VAULT_READ_ONLY=true;const b=vaultReadAllBar(ch);"
             "VAULT_READ_ONLY=false;const c=vaultReadAllBar([{chat_id:2}]);console.log(JSON.stringify([a,b,c]))")
    assert "Отметить все отвеченными (2)" in r[0] and r[1:] == ["", ""]


def test_handle_all_asks_then_marks_each():
    r = _run("_VAULT_CHATS=[{chat_id:4,awaiting_reply:true},{chat_id:5}];"
             "vaultHandleAll().then(()=>console.log(JSON.stringify({calls,toasts,reloads})))")
    assert r == {"calls": [["/api/miniapp/vault/chat/4/state", {"handled": True}]],
                 "toasts": ["✅ Отмечено отвеченными: 1"], "reloads": 1}


def test_declined_confirm_sends_nothing():
    r = _run("_VAULT_CHATS=[{chat_id:4,awaiting_reply:true}];"
             "vaultHandleAll().then(()=>console.log(JSON.stringify(calls)))", confirm="false")
    assert r == []
