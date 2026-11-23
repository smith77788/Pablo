"""Сумма сделки в CRM контакта (openContactCRM) раньше рисовалась в поле
`crm-value`, но не сохранялась нигде: setCrmStage слал только {stage},
saveCrmReminder — только {remind_at,text}. Введённая сумма терялась при первом
же перерисовывании экрана, хотя сервер (upsert_crm, поле deal_value — REAL в
schema_v148) её принимает. Регресс: поле обязано иметь триггер сохранения, и
функция сохранения обязана слать deal_value на ручку CRM.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")


def _html_fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_deal_value_input_has_save_trigger():
    body = _html_fn("openContactCRM")
    # поле суммы существует и имеет onchange, который зовёт сохранение
    assert 'id="crm-value"' in body
    assert "onchange=\"saveCrmValue('${id}')\"" in body


def test_save_deal_value_posts_to_crm_endpoint():
    body = _html_fn("saveCrmValue")
    # читает поле, шлёт POST на ручку CRM именно с deal_value
    assert "getElementById('crm-value')" in body
    assert "/api/miniapp/uch/contacts/'+id+'/crm'" in body
    assert "method:'POST'" in body
    assert "deal_value" in body
    # парсит число и не шлёт отрицательное/мусорное
    assert "parseFloat" in body


def test_other_crm_savers_did_not_swallow_deal_value():
    # setCrmStage/saveCrmReminder по-прежнему шлют только свои поля — сумму
    # сохраняет именно отдельный saveCrmValue, а не они.
    stage = _html_fn("setCrmStage")
    assert "deal_value" not in stage


def test_currency_picker_is_present_and_not_hardcoded_dollar():
    body = _html_fn("openContactCRM")
    # селектор валюты есть и сохраняет при смене; жёсткого «($)» в подписи нет
    assert 'id="crm-currency"' in body
    assert "crmCurrencyOptions(crm.currency)" in body
    assert "Сумма сделки ($)" not in body


def test_save_sends_currency_with_deal_value():
    body = _html_fn("saveCrmValue")
    assert "getElementById('crm-currency')" in body
    assert "currency" in body
    # валюта из закрытого списка, дефолт — не доллар
    assert "CRM_CURRENCIES" in body
    assert "'RUB'" in body


def test_currency_list_matches_deal_modal():
    # Список валют карточки CRM обязан совпадать с модалкой сделки на доске,
    # иначе одна и та же сделка в двух местах получает разные наборы валют.
    m = re.search(r"const CRM_CURRENCIES = \[([^\]]+)\]", HTML)
    assert m, "CRM_CURRENCIES not found"
    crm_cur = set(re.findall(r"'([A-Z]{3})'", m.group(1)))
    sel = re.search(r'<select id="dealCurrency"[\s\S]*?</select>', HTML)
    assert sel, "dealCurrency select not found"
    modal = set(re.findall(r"<option>([A-Z]{3})</option>", sel.group(0)))
    assert {"RUB", "USD", "EUR"} <= crm_cur
    assert crm_cur == modal, (crm_cur ^ modal)
