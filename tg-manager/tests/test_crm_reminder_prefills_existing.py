"""Карточка CRM контакта (openContactCRM): поля напоминания раньше рисовались
пустыми, даже когда напоминание уже стоит (оно показано в жёлтой плашке, но не
в полях). Владелец, желавший сдвинуть срок, видел пустое поле и либо вводил всё
заново, либо вслепую перезаписывал. Теперь поля предзаполнены текущим
напоминанием: время — через toLocalInput(new Date(utc)) (симметрично
localToUtcIso при сохранении, без сдвига пояса), текст — через esc.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")


def _html_fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_reminder_datetime_prefilled_from_utc_as_local():
    body = _html_fn("openContactCRM")
    assert 'id="crm-remind"' in body
    # предзаполняется текущим next_reminder_at, приведённым к локальному вводу
    assert "crm.next_reminder_at ? toLocalInput(new Date(crm.next_reminder_at))" in body


def test_reminder_text_prefilled():
    body = _html_fn("openContactCRM")
    assert 'id="crm-remind-text"' in body
    assert "esc(crm.next_reminder_text||'')" in body


def test_save_still_converts_local_to_utc():
    # предзаполнение локальным безопасно только потому, что сохранение
    # по-прежнему уводит время обратно в UTC — иначе был бы двойной сдвиг.
    save = _html_fn("saveCrmReminder")
    assert "localToUtcIso" in save


def test_toLocalInput_helper_exists():
    assert "function toLocalInput(" in HTML
