"""Знакомые персоны: при длинном списке есть поиск по имени, чату и заметке
(все слова запроса, без учёта регистра)."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/persona_peers.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_match_all_words_case_insensitive():
    js = _fn("ppPeerMatches") + (
        "const k='мария чат риелторов любит кофе 123';"
        "console.log(JSON.stringify([ppPeerMatches(k,'Мария кофе'),ppPeerMatches(k,'мария чай'),ppPeerMatches(k,''),ppPeerMatches(k,'123')]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, False, True, True]


def test_rows_carry_search_key_and_box_appears_on_long_list():
    html = _fn("personaPeersHtml")
    assert 'data-q="${esc(key)}"' in html and "peers.length > 5" in html and "ppFilter(this)" in html
