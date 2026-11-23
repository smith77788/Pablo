"""Доска сделок: сумму сделки раньше можно было задать только внутри карточки
контакта (CRM → «Сумма сделки»). На самой доске, где владелец работает со
сделками каждый день, она лишь показывалась. Теперь всплывающая карточка на
доске (dkOpenCard) даёт поле суммы + валюту и сохраняет тем же эндпойнтом, что
карточка контакта, обновляя суммы колонки по валютам по месту.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = (ROOT / "mini_app/screens/deals_board.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", DB)
    assert m, name
    return m.group(0)


def test_card_popup_has_value_and_currency_inputs():
    body = _fn("dkOpenCard")
    assert "dk-card-value" in body
    assert "dk-card-cur" in body
    # валюта берётся из общего списка карточки контакта, а не хардкодится
    assert "crmCurrencyOptions(c.currency)" in body
    assert "dkSaveCardValue(" in body


def test_save_posts_value_and_currency_to_crm_endpoint():
    body = _fn("dkSaveCardValue")
    assert "/api/miniapp/uch/contacts/' + cid + '/crm'" in body
    assert "method: 'POST'" in body
    assert "deal_value" in body and "currency" in body
    assert "parseFloat" in body


def test_save_updates_per_currency_column_totals_in_place():
    body = _fn("dkSaveCardValue")
    # суммы колонки считаются по валютам (by_currency) — старую вычесть, новую
    # прибавить, иначе шапка колонки врёт до перезагрузки
    assert "by_currency" in body
    assert "Math.max(0," in body
    assert "card.deal_value = deal_value" in body
