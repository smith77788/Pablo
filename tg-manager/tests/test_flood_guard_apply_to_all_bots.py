"""Защита от накрутки: настройки одного бота можно применить ко всем ботам.

Раньше режим и порог ставились на каждого бота отдельно — при десятке ботов
экран открывали десять раз. Кнопка «Применить ко всем ботам» шлёт те же
значения через тот же PATCH для каждого бота и честно считает сбои.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "flood_guard.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", JS)
    assert m, name
    return m.group(0)


def test_button_is_rendered_and_errors_are_russian():
    assert 'onclick="fgApplyAll()"' in JS
    assert "e.message" not in JS


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_apply_all_patches_every_bot_and_counts_failures():
    src = ("const FG_MODES=" + re.search(r"const FG_MODES = (\[[\s\S]*?\n\]);", JS).group(1) + ";\n"
           + _fn("_fgFormValues") + _fn("fgApplyAll") + r"""
let _fgBotId = 7, calls = [], toasts = [], reopened = 0;
const document = { querySelector: () => ({ value: 'protect' }),
                   getElementById: () => ({ value: '55' }) };
async function askConfirm(m) { calls.push(['confirm', m]); return true; }
function toast(m) { toasts.push(m); }
function errRu(e, f) { return f; }
function openFloodGuard() { reopened++; }
async function api(url, opts) {
  if (url === '/api/miniapp/bots') return { bots: [{ bot_id: 7 }, { bot_id: 8 }, { bot_id: 9 }] };
  calls.push([url, opts.method, JSON.parse(opts.body)]);
  if (url.endsWith('/9')) throw new Error('boom');
  return { ok: true };
}
fgApplyAll().then(() => console.log(JSON.stringify({ calls, toasts, reopened })));
""")
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    patches = [c for c in out["calls"] if c[0] != "confirm"]
    assert [p[0] for p in patches] == ["/api/miniapp/flood/7", "/api/miniapp/flood/8", "/api/miniapp/flood/9"]
    assert all(p[1] == "PATCH" and p[2] == {"mode": "protect", "threshold_per_min": 55} for p in patches)
    assert "3" in out["calls"][0][1]
    assert out["toasts"] == ["Готово: 2, не получилось: 1"]
    assert out["reopened"] == 1
