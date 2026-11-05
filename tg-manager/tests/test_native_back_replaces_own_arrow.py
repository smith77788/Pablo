"""Внутри Telegram «назад» — кнопка клиента, своя стрелка в шапке прячется.

Своя «←» дублировала кнопку «назад» Telegram и отнимала место у заголовка.
Прятать её можно только пока кнопка Telegram правда показана: вне Telegram,
на клиенте без BackButton (до Bot API 6.1) и на корневом экране своя стрелка
остаётся. Проверяется поведение: настоящая _syncBackButton в Node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function, miniapp_html

NODE = shutil.which("node")


def _run(stack: int, init_data: str, version_ok: bool, has_bb: bool = True) -> dict:
    prog = f"""
const cls = new Set(); let bbShown = null;
const document = {{ documentElement: {{ classList: {{ toggle(c, on) {{ on ? cls.add(c) : cls.delete(c); }} }} }} }};
const tg = {{ initData: {json.dumps(init_data)}, isVersionAtLeast() {{ return {json.dumps(version_ok)}; }},
  BackButton: {json.dumps(has_bb)} ? {{ show() {{ bbShown = true; }}, hide() {{ bbShown = false; }} }} : undefined }};
const STACK = new Array({stack}).fill('s');
{js_function("_nativeBackAvailable")}
{js_function("_syncBackButton")}
_syncBackButton();
console.log(JSON.stringify({{ hidden: cls.has('tg-back'), bbShown }}));
"""
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_inside_telegram_with_a_stack_own_arrow_hides():
    assert _run(2, "query_id=1", True) == {"hidden": True, "bbShown": True}


@pytest.mark.skipif(not NODE, reason="нужен node")
@pytest.mark.parametrize("stack,init,ver,bb", [
    (0, "query_id=1", True, True),    # корневой экран — стрелки и так нет
    (2, "", True, True),              # вне Telegram (PWA, браузер)
    (2, "query_id=1", False, True),   # старый клиент без BackButton
    (2, "query_id=1", True, False),   # SDK без BackButton
])
def test_own_arrow_stays_when_telegram_back_is_not_there(stack, init, ver, bb):
    assert _run(stack, init, ver, bb)["hidden"] is False


def test_css_hides_only_the_header_arrow():
    css = miniapp_html()
    assert re.search(r"html\.tg-back \.hdr > \.back\{display:none\}", css)
