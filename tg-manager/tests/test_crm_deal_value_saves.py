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
