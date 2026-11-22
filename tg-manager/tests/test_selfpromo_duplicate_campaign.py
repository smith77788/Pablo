"""Самопиар: «📋» у кампании открывает редактор с копией — тот же текст,
кнопка, боты, каналы и аудитория, но без id, то есть сохранится новой
кампанией со своей статистикой."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/selfpromo.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_copy_keeps_content_drops_identity_and_stats():
    js = _fn("spcCopyPreset") + (
        "const c={id:5,title:'Осень',target_kind:'channel',target_ref:'7',target_url:'https://t.me/x',body:'Текст',"
        "button_text:'Перейти',segment:'new',skip_target_audience:false,cooldown_days:3,auto_days:2,"
        "bot_ids:[1,2],channel_ids:[9],launches:4,delivered:100,status:'active'};"
        "const p=spcCopyPreset(c);c.bot_ids.push(3);console.log(JSON.stringify(p))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    p = json.loads(out.stdout)
    assert p["title"] == "Осень (копия)"
    assert p["bot_ids"] == [1, 2] and p["channel_ids"] == [9]
    assert p["body"] == "Текст" and p["segment"] == "new" and p["auto_days"] == 2
    for k in ("id", "launches", "delivered", "status"):
        assert k not in p


def test_duplicate_opens_editor_as_new_and_keeps_title():
    dup = _fn("spcDuplicate")
    assert "spcOpenEditor(null, spcCopyPreset(c))" in dup
    editor = _fn("spcOpenEditor")
    assert "((preset && preset.title) || '')" in editor
    assert 'onclick="spcDuplicate(${id})"' in SRC
