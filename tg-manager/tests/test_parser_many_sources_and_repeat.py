"""Парсер: несколько источников за раз, повтор прогона, люди прогона.

Форма брала один источник: десять каналов конкурентов — десять заходов в
форму с теми же типом и лимитом. Теперь источники перечисляются в одном поле,
и на каждый ставится свой прогон через ту же ручку /parser/submit. В истории
у прогона — «🔁 Повторить» (та же форма с источником и типом) и «👥 Люди»
(вкладка аудитории с фильтром по источнику).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

HTML = open(os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html"),
            encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"function %s\([^)]*\) \{.*?\n\}" % name, HTML, re.S)
    assert m, f"нет функции {name}"
    return m.group(0)


def test_sources_are_split_and_normalized():
    src = _fn("_parserSources")
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = src + "\nconsole.log(JSON.stringify(_parserSources('@a https://t.me/bbb/\\nccc, @a')))"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["a", "bbb", "ccc"]


def test_submit_loops_over_sources_through_one_route():
    body = _fn("submitParser")
    assert "for (const src of srcs)" in body
    assert body.count("/api/miniapp/parser/submit") == 1


def test_history_row_offers_repeat_and_people():
    runs = _fn("loadParserRuns")
    assert "repeatParserRun(${r.id})" in runs and "showParserRunAudience(${r.id})" in runs
    assert "loadParsedAudience(r.source" in _fn("showParserRunAudience")
