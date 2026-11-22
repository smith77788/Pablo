"""Правила редактора: под рубриками сразу видны доли в процентах и ошибки —
тем же разбором, что у сервера (validate_pillars), чтобы «Новости: 3» не
читалось как «3%»."""
import json
import re
import subprocess
from pathlib import Path

from services import channel_brain_store as S

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/editorial.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _shares(text):
    js = _fn("_edPillarShares") + "console.log(JSON.stringify(_edPillarShares(%s)))" % json.dumps(text)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_weights_become_percentages():
    r = _shares("Новости: 3\nПолезное: 2\n\nРеклама: 1")
    assert [(x["name"], x["pct"]) for x in r["items"]] == [("Новости", 50), ("Полезное", 33), ("Реклама", 17)]
    assert r["errors"] == []


def test_same_names_and_weights_as_server():
    for text in ["Итоги: неделя\nНовости:  2", "А: 11\nБ: 0\nВ", "Новости: 1\nновости: 2", "  Много   пробелов : 4"]:
        names, weights, errors = S.validate_pillars(text)
        r = _shares(text)
        assert [x["name"] for x in r["items"]] == names, text
        assert {x["name"]: float(x["weight"]) for x in r["items"]} == weights, text
        assert len(r["errors"]) == len(errors), text


def test_preview_is_wired_to_the_textarea():
    assert 'id="edPillars" rows="4" oninput="edPillarsPreview()"' in SRC
    assert 'id="edPillarsPrev"' in SRC
