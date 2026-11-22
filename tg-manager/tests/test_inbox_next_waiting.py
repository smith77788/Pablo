"""Входящие: из разговора — кнопка «⏭ Следующий ждущий ответа» (по кругу,
без текущего), а отправленный ответ снимает «ждёт» с разговора."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/crm_inbox.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = _fn("inboxNextWaiting") + body
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_next_waiting_wraps_and_skips_current():
    r = _run(
        "const rows=[{waiting:true},{waiting:false},{waiting:true},{waiting:false}];"
        "console.log(JSON.stringify([inboxNextWaiting(rows,0),inboxNextWaiting(rows,2),"
        "inboxNextWaiting(rows,1),inboxNextWaiting([{waiting:true}],0),inboxNextWaiting([],0)]))"
    )
    assert r == [2, 0, 2, -1, -1]


def test_thread_offers_next_and_reply_clears_waiting():
    thread = _fn("openInboxThread")
    assert "inboxNextWaiting(_ibRows, i)" in thread and "Следующий ждущий ответа" in thread
    send = _fn("sendInboxReply")
    assert send.index("t.waiting = false;") < send.index("openInboxThread(i)")
