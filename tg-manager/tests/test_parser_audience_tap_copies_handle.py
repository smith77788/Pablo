"""Аудитория парсера: тап по человеку копирует @username, а без него — ID."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_handle_prefers_username_then_id():
    js = _fn("audHandle") + (
        "console.log(JSON.stringify([audHandle({username:'ivan',tg_user_id:5}),audHandle({tg_user_id:77}),"
        "audHandle({}),audHandle(null)]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["@ivan", "77", "", ""]


def test_rows_are_tappable():
    body = _fn("loadParsedAudience")
    assert 'onclick="audCopyOne(${i})"' in body
    assert "copyToClipboard(h" in _fn("audCopyOne")
