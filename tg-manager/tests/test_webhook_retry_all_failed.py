"""Получатель вебхуков: отбор «только неудачные» и повтор всех неудачных
доставок одной кнопкой той же ручкой, что у кнопки строки."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/integrations.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = (
        "const calls=[],toasts=[];let reopened=0;let _igWhId=4;"
        "const plural=(n,a,b,c)=>c;const askConfirm=async()=>true;const toast=m=>toasts.push(m);"
        "function openWebhook(){reopened++;}function _igRenderWebhook(){}"
        "async function api(p,o){calls.push(p);if(p.includes('/9/'))throw new Error('x');}"
        + _fn("igFailedDeliveries") + _fn("igToggleFailedOnly") + _fn("igRedeliverFailed") + body
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_failed_are_failed_and_skipped_only():
    r = _run(
        "console.log(JSON.stringify(igFailedDeliveries([{id:1,status:'delivered'},{id:2,status:'failed'},"
        "{id:3,status:'pending'},{id:4,status:'skipped'},null]).map(x=>x.id)))"
    )
    assert r == [2, 4]


def test_retry_all_failed_sequentially_and_counts():
    r = _run(
        "_igRenderWebhook.items=[{id:8,status:'failed'},{id:5,status:'delivered'},{id:9,status:'skipped'}];"
        "igRedeliverFailed().then(()=>console.log(JSON.stringify({calls,toasts,reopened})))"
    )
    assert r["calls"] == ["/api/miniapp/webhook_delivery/8/retry", "/api/miniapp/webhook_delivery/9/retry"]
    assert r["toasts"] == ["⏳ В очереди: 1, не получилось: 1"]
    assert r["reopened"] == 1


def test_filter_toggles_and_list_uses_it():
    r = _run("igToggleFailedOnly();const a=_igRenderWebhook.failedOnly;igToggleFailedOnly();"
             "console.log(JSON.stringify([a,_igRenderWebhook.failedOnly,reopened]))")
    assert r == [True, False, 2]
    assert "const shown = _igRenderWebhook.failedOnly && failed.length ? failed : items;" in SRC
    assert "h += shown.map(it =>" in SRC
