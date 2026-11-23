"""Ctrl/⌘+Enter в поле ответа (Хранилище, Входящие) отправляет сообщение;
обычный Enter в многострочном поле оставляет перенос строки."""
import json
import re
import subprocess
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")
INBOX = (Path(__file__).resolve().parents[1] / "mini_app/screens/crm_inbox.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_only_ctrl_or_meta_enter_fires():
    js = _fn("sendOnCtrlEnter") + (
        "let fired=0;const call=k=>sendOnCtrlEnter(k,()=>fired++);"
        "const ev=(key,mod)=>({key,ctrlKey:mod==='ctrl',metaKey:mod==='meta',preventDefault(){}});"
        "const r=[call(ev('Enter')),call(ev('Enter','ctrl')),call(ev('Enter','meta')),call(ev('a','ctrl'))];"
        "console.log(JSON.stringify([r,fired]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    rets, fired = json.loads(out.stdout)
    # plain Enter and ctrl+'a' pass through (return true); ctrl/meta+Enter handled (false)
    assert rets == [True, False, False, True]
    assert fired == 2  # only ctrl+Enter and meta+Enter


def test_reply_boxes_are_wired():
    assert 'onkeydown="sendOnCtrlEnter(event, sendVaultReply)"' in HTML
    assert "sendOnCtrlEnter(event, sendInboxReply)" in INBOX


def test_vault_reply_autogrows_and_resets():
    assert 'oninput="autoGrowInput(this)"' in HTML
    send = _fn("sendVaultReply")
    assert "autoGrowInput(inp)" in send
    # высота не превышает maxHeight поля
    js = _fn("autoGrowInput") + (
        "const el={style:{maxHeight:'120px',height:''},scrollHeight:300};autoGrowInput(el);"
        "console.log(el.style.height);"
        "const el2={style:{maxHeight:'120px',height:''},scrollHeight:40};autoGrowInput(el2);"
        "console.log(el2.style.height)")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["120px", "40px"]
