"""Экран напоминаний: список того, что просрочено, и ничего нельзя сделать.

«⏰ Напоминания» собирает всё, о чём владелец просил себе напомнить по
контактам. Экран показывал имя, текст и дату — и ни одной кнопки: ни открыть
контакт, о котором речь, ни отметить сделанным, ни отложить. Просроченное
напоминание висело вечно, и единственным способом его снять было найти контакт
руками в другом разделе и перезаписать дату.

Возможность была: `POST /api/miniapp/uch/contacts/{id}/crm/reminder` принимает
и новую дату, и пустую (тогда напоминание снимается), а в самих строках уже
лежит `contact_id` — экран его просто не использовал.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _screen() -> str:
    html = _read("mini_app/index.html")
    i = html.index("async function openUchReminders()")
    return html[i:html.index("function openUchSyncCenter()", i)]


def test_probe_sees_the_rows():
    body = _screen()
    assert "overdue" in body and "upcoming" in body, "строки напоминаний не находятся"


def test_reminder_opens_the_contact_it_is_about():
    body = _screen()
    assert "openContactDetail(" in body, (
        "из напоминания нельзя попасть к контакту, о котором оно")


def test_reminder_can_be_closed_and_postponed():
    html = _read("mini_app/index.html")
    assert "function remDone(" in html, "напоминание нельзя отметить сделанным"
    assert "function remSnooze(" in html, "напоминание нельзя отложить"
    done = html[html.index("async function remDone("):]
    done = done[:done.index("\nasync function ", 10)]
    assert "remind_at: null" in done or '"remind_at":null' in done.replace(" ", ""), (
        "«готово» обязано снимать напоминание, а не ставить новое")


def test_actions_pass_an_index_not_the_contact_text():
    """Имя контакта — внешний текст, в onclick ему не место."""
    body = _screen()
    for m in re.finditer(r"onclick=\"(rem\w+)\(([^\"]*)\)", body):
        assert re.fullmatch(r"\d+|\$\{\w+\}|\$\{\w+\},\s*-?\d+", m.group(2).strip()), (
            f"в обработчик {m.group(1)} уходит не индекс: {m.group(2)!r}")


def test_backend_accepts_clearing_a_reminder():
    api = _read("services/mini_app_api.py")
    i = api.index("async def uch_crm_reminder")
    fn = api[i:api.index("\n    async def ", i + 10)]
    assert "remind_dt = None" in fn, (
        "снять напоминание нечем: пустая дата обязана обнулять поле")


def test_engine_keeps_the_username_for_writing_to_the_person():
    src = _read("services/contacts_hub/crm_engine.py")
    for name in ("get_upcoming_reminders", "get_crm_overdue"):
        i = src.index(f"async def {name}")
        fn = src[i:src.index("\nasync def ", i + 10)]
        assert "d.pop('username'" not in fn, (
            f"{name} выбрасывает username, когда имя непустое — написать "
            f"человеку из напоминания становится нечем")
