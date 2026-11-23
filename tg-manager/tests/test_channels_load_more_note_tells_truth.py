"""Подпись под списком каналов при поиске не утверждает, что искали только
среди загруженных: поиск и срез давно делает сервер (test_channels_search_is_
server_side), и CH_TOTAL — число найденных по всем каналам."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_note_counts_found_over_all_channels():
    body = _fn("renderChLoadMore")
    assert "среди загруженных" not in body
    assert "Найдено ${CH_TOTAL} по всем каналам." in body
    assert "p.set('search', CH_SEARCH)" in _fn("_chQuery")
