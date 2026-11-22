"""Доска сделок: быстрый отбор по загруженным карточкам — поиск по имени,
компании, нику и названию сделки и чип «⏰ Пора» (напоминание уже наступило)."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/deals_board.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = ("let _DK_Q='', _DK_DUE=false;" + _fn("_dkFiltering") + _fn("_dkMatch") + body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_query_matches_contact_fields_and_deal_title():
    r = _run(
        "const c={name:'Иван Петров',company:'Ромашка',username:'ivan_p'};"
        "const d={title:'Сайт под ключ',contact:'Ольга'};const out=[];"
        "for (const q of ['ромаш','@ivan','петров','сайт','ольга','нет такого']) {"
        " _DK_Q=q; out.push([_dkMatch(c,false),_dkMatch(d,true)]); }"
        "_DK_Q=''; out.push(_dkFiltering());"
        "console.log(JSON.stringify(out))"
    )
    assert r == [[True, False], [True, False], [True, False], [False, True],
                 [False, True], [False, False], False]


def test_due_chip_keeps_only_reached_reminders():
    r = _run(
        "_DK_DUE=true;const past=new Date(Date.now()-3600e3).toISOString();"
        "const fut=new Date(Date.now()+3600e3).toISOString();"
        "console.log(JSON.stringify([_dkMatch({name:'a',next_reminder_at:past},false),"
        "_dkMatch({name:'b',next_reminder_at:fut},false),_dkMatch({name:'c'},false),"
        "_dkMatch({title:'d'},true),_dkFiltering()]))"
    )
    assert r == [True, False, False, False, True]


def test_board_renders_filtered_cards_and_explains_empty_column():
    assert "cards.map(_dkCard).join('') + deals.map(_dkDealCard)" in SRC
    assert "Не подходит под отбор" in SRC
    # Поле поиска вне dkTop — перерисовка доски не сбивает набор.
    assert SRC.index('id="dkQ"') < SRC.index("'<div id=\"dkBody\">'")
