"""Доска сделок: «+» в шапке колонки заводит сделку сразу в стадии этой
колонки (openDealModal(null, stage)), а не в «Лид» с последующим переносом."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = (ROOT / "mini_app/screens/deals_board.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")


def _html_fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_column_header_has_add_button():
    assert "dkAddToStage(\\'' + esc(col.stage) + '\\')" in DB
    assert "function dkAddToStage(stage)" in DB
    assert "openDealModal(null, stage)" in DB


def test_modal_honours_prestage_for_new_only():
    body = _html_fn("openDealModal")
    # новая сделка берёт preStage; у правки стадия из самой сделки и поле заперто
    assert "deal ? (deal.stage||'lead') : (preStage || 'lead')" in body
    assert "ds.disabled=!!deal" in body
