"""«Найдено у вас» (find_mine.js): кнопка «Ещё» у группы «Сделки» раньше
открывала доску сделок БЕЗ запроса (_FM_MORE['deals'] = 'openDeals()'), хотя у
доски есть своя строка поиска (#dkQ → dkSetQuery). Из 40 найденных сделок
владелец видел все и набирал слово второй раз — ровно тот случай, под который
сделан _FM_MORE_Q. Регресс: у deals обязан быть проброс запроса, и он обязан
зваться через dkSetQuery/#dkQ, которые на доске и есть.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FM = (ROOT / "mini_app/screens/find_mine.js").read_text(encoding="utf-8")
DB = (ROOT / "mini_app/screens/deals_board.js").read_text(encoding="utf-8")


def _fm_more_q_block():
    m = re.search(r"const _FM_MORE_Q = \{[\s\S]*?\n\};", FM)
    assert m, "_FM_MORE_Q not found"
    return m.group(0)


def test_deals_has_a_query_passthrough():
    block = _fm_more_q_block()
    assert re.search(r"\bdeals:\s*q\s*=>", block), "deals passthrough missing in _FM_MORE_Q"


def test_deals_passthrough_opens_board_and_sets_query():
    block = _fm_more_q_block()
    deals = re.search(r"deals:\s*q\s*=>\s*\{[^}]*\}", block)
    assert deals, "deals arrow body not found"
    body = deals.group(0)
    assert "openDeals()" in body
    # запрос доезжает именно до строки поиска доски
    assert "dkSetQuery" in body
    assert "'dkQ'" in body


def test_board_actually_has_that_search_input_and_setter():
    # проброс не должен целиться в несуществующее поле
    assert 'id="dkQ"' in DB
    assert "function dkSetQuery(" in DB
