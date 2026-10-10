"""Список контактов листается до конца, а сбор берёт больше диалогов.

Два дефекта при 10к+ контактов: (1) список молча обрывался дефолтным limit=100
без пагинации — видно «лишь десятки» при тысячах в БД; (2) сбор из диалогов был
ограничен 500 на аккаунт → флот отдавал малую часть (2.9к вместо 10к+).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
SYNC = (ROOT / "services" / "contacts_hub" / "sync_service.py").read_text(encoding="utf-8")


def test_endpoint_supports_pagination():
    m = re.search(r"async def uch_contacts\(.*?\n    async def ", API, re.S)
    assert m, "uch_contacts не найден"
    body = m.group(0)
    assert "request.query.get('limit'" in body and "request.query.get('offset'" in body, \
        "эндпойнт не принимает limit/offset"
    assert "limit=limit, offset=offset" in body, "limit/offset не проброшены в get_contacts"


def test_frontend_paginates_with_load_more():
    m = re.search(r"async function loadContacts\(.*?\n\}", HTML, re.S)
    assert m, "loadContacts не найден"
    body = m.group(0)
    assert "CONTACT_OFFSET" in body, "нет смещения пагинации"
    assert "loadContacts(true)" in body, "нет догрузки следующей страницы"
    assert "Показать ещё" in body, "нет кнопки «Показать ещё»"
    assert "set('offset'" in body and "set('limit'" in body, "фронт не шлёт limit/offset"


def test_harvest_depth_raised():
    """Сбор диалогов берёт заметно больше 500 на аккаунт."""
    m = re.search(r"get_dialog_contacts\(\s*acc\['session_str'\],\s*limit=(\d+)", SYNC)
    assert m, "sync не задаёт явный limit сбора диалогов"
    assert int(m.group(1)) >= 2000, f"лимит сбора диалогов слишком мал: {m.group(1)}"
