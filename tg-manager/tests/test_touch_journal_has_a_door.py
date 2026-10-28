"""Журнал касаний контакта можно ЗАПОЛНИТЬ, а не только показать.

НАХОДКА (22.10.2026). Экран CRM контакта рисовал секцию «Активность» из
`d.activity` — и ровно это было всё: записывает в журнал одна функция,
`crm_engine.log_crm_activity`, её зовёт одна ручка,
POST /api/miniapp/uch/contacts/{id}/crm/activity, и эту ручку не звал ни
один экран. То есть журнал был пуст у всех контактов всегда, секция не
показывалась ни разу, а таблица, движок и их юнит-тесты были на месте.
Тот же класс, что пустой `trust_score` (см.
tests/test_contact_trust_is_actually_computed.py).

ВТОРАЯ ПОЛОВИНА НАХОДКИ — ЯЗЫК. Строки журнала рисовались как
`esc(a.activity_type)`, то есть владельцу, который не читает по-английски,
в журнале стояло бы «call» и «meeting». Подписи теперь считает сервер
(`ACTIVITY_LABELS`), экран показывает `activity_label`.

ТРЕТЬЯ — ДВА ОПАСНЫХ ДУБЛЯ РЯДОМ. `create_crm_activity` писал в тот же
журнал БЕЗ `_require_contact`, то есть по чужому contact_id, и выглядел
удобнее настоящего (возвращал id); `update_crm_stage` умел меньше, чем
upsert_crm (не нормализовал поля и не поднимал сигнал виртуального слоя).
Не звал их никто, кроме тестов. Удалены: следующий, кто подключал бы
журнал, с высокой вероятностью взял бы первый — и завёл дыру заново.
"""
from __future__ import annotations

import os
import re

from tests.miniapp_source import js_function, miniapp_html, py_function

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_screen_can_add_a_touch():
    assert "addCrmTouch(" in miniapp_html(), "в CRM контакта нечем отметить касание"
    body = js_function("addCrmTouch")
    assert "/crm/activity" in body, "кнопка не зовёт ручку журнала"
    assert "method:'POST'" in body.replace(" ", ""), "журнал только читается"
    assert "askChoice" in body, "тип касания вводят строкой, а сервер её не примет"
    assert "openContactCRM(id)" in body, (
        "экран не перечитан: запись выглядела бы как «ничего не произошло»")
    assert "desc === null" in body, (
        "отмена ввода описания запишет пустое касание")


def test_the_button_is_there_even_when_the_journal_is_empty():
    """Кнопка внутри `if (activity.length)` — это замок на пустой комнате.

    Журнал пуст у каждого контакта, у которого ещё не было касаний, то есть
    у всех: вход, видимый только при непустом журнале, не даёт сделать
    первую запись никогда.
    """
    body = js_function("openContactCRM")
    i = body.index("addCrmTouch")
    j = body.index("if (activity.length)")
    assert i < j, "кнопка «Отметить касание» спрятана за непустым журналом"


def test_the_label_comes_from_the_server():
    body = js_function("openContactCRM")
    assert "a.activity_label" in body, (
        "в журнале снова английский activity_type — владелец читает по-русски")
    labels = py_function("services/contacts_hub/crm_engine.py", "activity_label")
    assert "ACTIVITY_LABELS" in labels
    src = open(os.path.join(ROOT, "services", "contacts_hub", "crm_engine.py"),
               encoding="utf-8").read()
    m = re.search(r"ACTIVITY_LABELS = \{(.+?)\}", src, re.DOTALL)
    assert m, "закрытого списка типов нет"
    for word in ("Звонок", "Встреча", "Заметка"):
        assert word in m.group(1), f"нет подписи {word}"


def test_the_type_is_a_closed_list_on_the_server():
    """Тип приходит с экрана и ложится в базу.

    Свободная строка превращает журнал в свалку, по которой нельзя ни
    посчитать, ни сгруппировать, — и ни один экран не починит это потом.
    """
    fn = py_function("services/mini_app_uch.py", "uch_crm_activity")
    assert "ACTIVITY_LABELS" in fn, "тип касания не проверяется по списку"
    assert "Неизвестный тип касания" in fn, "отказ не по-русски или его нет"
    assert "max_len=500" in fn, "описание не ограничено по длине"


def test_the_journal_has_one_reader_and_one_writer():
    """Вторая дверь к тому же списку расходится с первой на первой же правке.

    GET .../crm/activity отдавал тот же журнал, что и GET .../crm (там он
    лежит в поле activity), и его не звал никто: подписи добавились бы в
    одну дверь и не добавились бы в другую.
    """
    src = open(os.path.join(ROOT, "services", "mini_app_uch.py"),
               encoding="utf-8").read()
    assert src.count('crm/activity"') == 1, (
        "у журнала снова две двери на чтение")
    assert 'add_post("/api/miniapp/uch/contacts/{contact_id}/crm/activity"' in src

    import services.contacts_hub.crm_engine as ce
    for dead in ("create_crm_activity", "update_crm_stage"):
        assert not hasattr(ce, dead), (
            f"{dead} вернулся: это дубль, и первый из них писал в журнал без "
            "проверки владельца контакта")
