"""Тест цены: «Новый тест на основе этого» и память формы.

После победы следующую ступень цены ставили вручную, перенабирая бота, тип
и цены; форма к тому же не помнила последнего бота. Проверяем поведение
настоящей функции из index.html в node, а не её текст.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\nfunction " + name + r"\(\)[\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def _repeat(exp):
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = ("let _SX_CUR = %s; let got = null;\n"
          "function openStarsExpModal(p) { got = p; }\n%s\n"
          "repeatStarsExp(); console.log(JSON.stringify(got));") % (json.dumps(exp), _fn("repeatStarsExp"))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_winner_price_goes_against_the_next_step():
    got = _repeat({"bot_id": 7, "content_type": "post", "name": "Пост", "price_a": 50,
                   "price_b": 100, "winner": "b"})
    assert got["bot_id"] == 7 and got["content_type"] == "post"
    assert got["price_a"] == 100 and got["price_b"] == 133
    assert "повтор" in got["name"]


def test_no_winner_keeps_the_same_prices():
    got = _repeat({"bot_id": 7, "content_type": "post", "name": "Пост", "price_a": 50,
                   "price_b": 100, "winner": None})
    assert (got["price_a"], got["price_b"]) == (50, 100)


def test_tiny_winner_price_still_differs():
    got = _repeat({"bot_id": 1, "content_type": "post", "name": "x", "price_a": 1,
                   "price_b": 2, "winner": "a"})
    assert got["price_b"] > got["price_a"]


def test_detail_offers_the_button_and_form_remembers_bot():
    assert 'onclick="repeatStarsExp()"' in HTML
    assert "saveFormPrefs('starsexp'" in HTML
    assert "applyFormPrefs('starsexp'" in HTML
