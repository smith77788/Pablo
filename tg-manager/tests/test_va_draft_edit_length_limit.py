"""Правка черновика Администратора: счётчик знаков и запрет публикации сверх
предела. С фото предел — серверный va_media.CAPTION_LIMIT, без фото — 4096."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "mini_app/screens/va_admin.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_limit_matches_server_caption_limit():
    from services import va_media
    m = re.search(r"function vaDraftLimit\(hasMedia\) \{ return hasMedia \? (\d+) : (\d+); \}", SRC)
    assert m and int(m.group(1)) == va_media.CAPTION_LIMIT and int(m.group(2)) == 4096


def test_counter_disables_publish_when_over():
    stub = (
        "const els={};function mk(id){return els[id]=els[id]||{id,value:'',innerHTML:'',dataset:{},disabled:false,"
        "querySelector(){return els.pub}}};els.pub={disabled:false};"
        "const document={getElementById:id=>mk(id)};"
        "function vaDraftLimit(m){return m?900:4096}"
    )
    js = stub + _fn("vaEdCount") + (
        "mk('vaBodych-7').dataset.media='1';mk('vaEdch-7').value='x'.repeat(901);vaEdCount(7,'ch');"
        "const a=[els.pub.disabled,els['vaEdNch-7'].innerHTML.includes('901 / 900')];"
        "mk('vaEdch-7').value='x'.repeat(900);vaEdCount(7,'ch');a.push(els.pub.disabled);"
        "console.log(JSON.stringify(a))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, True, False]


def test_draft_card_marks_media_and_editor_counts():
    assert "data-media=\"1\"" in _fn("_vaDraftsHtml")
    ed = _fn("vaDraftEdit")
    assert "vaEdCount(" in ed and "oninput=" in ed
