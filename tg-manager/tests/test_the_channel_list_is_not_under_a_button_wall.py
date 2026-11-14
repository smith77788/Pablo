"""Список каналов не прячется под стеной кнопок.

Живой обход (03.11.2026): над списком «Каналы и чаты» стояло шесть рядов —
десять кнопок, и сам список начинался ниже середины экрана телефона. Половина
из них — редкое и служебное (импорт, обновление данных, чистка чужих,
удалённые), а пояснения к ним жили в `title`, которого на телефоне не видно.

Теперь на виду два ряда частого, а редкое — в «⋯ Ещё» с подписью под каждым
действием. Меню проверяется поведением: каждый пункт зовёт ровно своё
действие.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function, miniapp_html

NODE = shutil.which("node")


def _channels_screen() -> str:
    html = miniapp_html()
    i = html.index('id="s-channels"')
    return html[i:html.index('id="s-', i + 10)]


def test_at_most_two_rows_of_buttons_above_the_list():
    head = _channels_screen().split('class="inp lst-search"')[0] if 'lst-search' in _channels_screen() \
        else _channels_screen().split('id="chSearch"')[0]
    buttons = re.findall(r"<button\b", head)
    assert len(buttons) <= 6, f"над списком каналов снова {len(buttons)} кнопок"
    assert 'onclick="chMoreActions()"' in head


@pytest.mark.skipif(not NODE, reason="нужен node")
@pytest.mark.parametrize("choice,expected", [
    ("imp_ch", "importAllResources:channels"),
    ("imp_gr", "importAllResources:groups"),
    ("refresh", "refreshChannelsMeta"),
    ("mine", "reclassifyChannels"),
    ("problems", "openRestrictionCleanup"),
    ("removed", "openRemovedChannels"),
    (None, None),
])
def test_every_menu_item_calls_its_action(choice, expected):
    prog = f"""
const called = [];
let offered = [];
async function askChoice(t, opts) {{ offered = opts.map(o => o.value); return {json.dumps(choice)}; }}
function importAllResources(k) {{ called.push('importAllResources:' + k); }}
function refreshChannelsMeta() {{ called.push('refreshChannelsMeta'); }}
function reclassifyChannels() {{ called.push('reclassifyChannels'); }}
function openRestrictionCleanup(x) {{ called.push('openRestrictionCleanup'); }}
function openRemovedChannels() {{ called.push('openRemovedChannels'); }}
{js_function("chMoreActions")}
(async () => {{ await chMoreActions(); console.log(JSON.stringify({{called, offered}})); }})();
"""
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["called"] == ([expected] if expected else []), res
    assert len(res["offered"]) == 6


def test_choice_rows_put_the_hint_under_the_label():
    # У .btn строка-flex: без display:block подпись вставала сбоку от названия.
    assert "display:block;width:100%" in js_function("askChoice")
