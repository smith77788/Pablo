"""Менеджер продаж, администратор канала и ссылка-приглашение контакта
показывают ошибки через errRu: сырой e.message уходил на экран английским
текстом сервера или браузера («Failed to fetch»)."""
import re
from pathlib import Path

SCREENS = Path(__file__).resolve().parents[1] / "mini_app" / "screens"


def test_no_raw_error_message_left():
    for name in ("sales_manager.js", "va_admin.js", "contact_invite_link.js"):
        src = (SCREENS / name).read_text(encoding="utf-8")
        assert "e.message" not in src, name


def test_no_glued_identifier_from_the_rewrite():
    # «toasterrRu(» синтаксически верен и молча роняет обработчик ошибки.
    for f in SCREENS.glob("*.js"):
        assert not re.search(r"[A-Za-z0-9_$]errRu\(", f.read_text(encoding="utf-8")), f.name
