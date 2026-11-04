"""Причина состояния рассылки видна на её строке в мини-аппе.

broadcasts.status_note (schema_v297) объясняет по-русски, почему рассылка ждёт,
почему остановилась и кому не дошла. Ручки списка рассылок и карточки бота
обязаны его отдавать, а строка — показывать, когда оно не пустое. Строка
проверяется поведением: настоящая renderBcasts исполняется в Node.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function

NODE = shutil.which("node")
NOTE = "Токен бота отозван — подключите бота заново"


def test_both_endpoints_select_the_note():
    root = pathlib.Path(__file__).resolve().parents[1]
    assert "b.status_note" in (root / "services" / "mini_app_broadcast.py").read_text(encoding="utf-8")
    assert "status_note" in (root / "services" / "mini_app_bots.py").read_text(encoding="utf-8")


def _render(rows: list[dict]) -> str:
    prog = f"""
const els = {{}};
const document = {{ getElementById(id) {{ return els[id] || (els[id] = {{ innerHTML: '', className: '', textContent: '', style: {{}} }}); }} }};
let BCAST_TOTAL = 1, BCAST_FILTER = '', BCAST_ROWS = [];
function _bcastShownTotal() {{ return 1; }}
function renderBcastChips() {{}} function renderBcastMore() {{}}
function empty() {{ return ''; }}
function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
function num(n) {{ return String(n); }}
function stb(s) {{ return ['b-gy', s]; }}
{js_function("renderBcasts")}
renderBcasts({json.dumps(rows)});
console.log(JSON.stringify(els.bcastList.innerHTML));
"""
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_ROW = {"id": 1, "bot_id": 2, "bot_username": "shop_bot", "message_text": "Привет",
        "status": "failed", "sent_count": 3, "failed_count": 5}


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_note_is_shown_and_escaped():
    html = _render([dict(_ROW, status_note=NOTE + " <b>")])
    assert NOTE in html and 'class="bc-note"' in html
    assert "<b>" not in html.split('class="bc-note"', 1)[1], "причина не экранирована"


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_no_note_no_empty_line():
    assert "bc-note" not in _render([dict(_ROW, status_note=None)])
    assert "bc-note" not in _render([dict(_ROW, status_note="")])
