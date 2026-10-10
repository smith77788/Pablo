"""Паритет: ручное добавление CRM-контакта в боте (crm_contact_create).

Раньше добавить контакт вручную можно было только из mini-app
(POST /api/miniapp/crm/contact). Бот получил кнопку «👤 Новый контакт» и FSM
с тем же upsert в crm_contacts (source='manual').
"""
from __future__ import annotations

import os

from bot.handlers.crm import _parse_contact_line

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_parse_full_pipe_form():
    fn, ln, un, ph = _parse_contact_line("Иван Петров | @ivan | +79991234567")
    assert fn == "Иван Петров"
    assert un == "ivan"           # @ снят
    assert ph == "+79991234567"


def test_parse_single_username():
    fn, ln, un, ph = _parse_contact_line("@ivan")
    assert un == "ivan" and fn is None and ph is None


def test_parse_single_phone():
    fn, ln, un, ph = _parse_contact_line("+7 (999) 123-45-67")
    assert ph == "+7 (999) 123-45-67" and un is None and fn is None


def test_parse_single_name():
    fn, ln, un, ph = _parse_contact_line("Иван Петров")
    assert fn == "Иван Петров" and un is None and ph is None


def test_parse_empty_gives_all_none():
    assert _parse_contact_line("   ") == (None, None, None, None)


def test_parse_partial_pipe_skips_fields():
    fn, ln, un, ph = _parse_contact_line("Иван |  | +70000000000")
    assert fn == "Иван" and un is None and ph == "+70000000000"


def test_menu_button_and_handlers_wired():
    h = _read("bot/handlers/crm.py")
    assert 'CrmCb(action="contact_add")' in h
    assert 'CrmCb.filter(F.action == "contact_add")' in h
    assert "AddContact.waiting_info" in h


def test_insert_uses_manual_source_upsert():
    h = _read("bot/handlers/crm.py")
    assert "INSERT INTO crm_contacts" in h
    assert "'manual'" in h
    assert "ON CONFLICT (owner_id, tg_user_id) DO UPDATE" in h


def test_app_endpoint_still_present():
    api = _read("services/mini_app_api.py")
    assert "async def crm_contact_create" in api
