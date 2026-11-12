"""Один человек — один ключ: телефон и @ник в объединённой базе контактов.

НАХОДКА ПЕРВАЯ. Поиск дублей сравнивал телефоны и ники СТРОКОЙ. База
сливается из книги аккаунта (Telegram отдаёт `79991234567` без «+»), ручного
ввода (`8 (999) 123-45-67`), импорта (`+7 999 123 45 67`) и парсера (`@Ivan`
против `ivan`). Для поиска это были разные люди: настоящий двойник не попадал
ни в одну группу кандидатов и на экран «Дубликаты» не выходил вообще.

НАХОДКА ВТОРАЯ. Слияние объединяло телефоны строкой — в карточке оставалось
по три записи одного номера.

НАХОДКА ТРЕТЬЯ, самая дорогая. Синк контактов писал
`phones = [номер из Telegram]` поверх существующего списка. У собеседника из
диалогов Telegram номера не отдаёт вовсе, поэтому КАЖДЫЙ синк стирал номера,
внесённые владельцем вручную, импортом или слиянием дублей.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. вернуть `str(x)` вместо phone_keys в compute_merge_confidence ->
     красные test_one_number_in_different_spellings_is_one_phone и
     test_the_screen_finds_a_twin_written_differently;
  2. вернуть `.lower()` вместо username_key -> красный
     test_at_sign_and_case_do_not_make_two_people;
  3. вернуть объединение строкой в absorbed_fields -> красный
     test_merge_does_not_triple_one_number;
  4. вернуть `SELECT id` и прямую запись phones в синке -> красный
     test_sync_keeps_phones_the_owner_added_postgres.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from services.contacts_hub import trust_engine
from services.contacts_hub.identity_keys import (
    merge_phones, phone_key, username_key,
)
from services.contacts_hub.merge_engine import absorbed_fields
from services.contacts_hub.trust_engine import compute_merge_confidence


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("raw", [
    "+79991234567", "79991234567", "89991234567", "8 (999) 123-45-67",
    "+7 999 123 45 67", "9991234567",
])
def test_russian_spellings_share_one_key(raw):
    assert phone_key(raw) == "79991234567"


@pytest.mark.parametrize("raw", ["", None, "0", "123", "-", "1234567890123456"])
def test_garbage_is_not_a_phone(raw):
    """Мусор импорта не должен склеивать в одну группу всех, у кого он записан."""
    assert phone_key(raw) == ""


def test_foreign_numbers_are_kept_as_digits():
    assert phone_key("+1 (415) 555-0100") == "14155550100"
    assert phone_key("+44 20 7946 0958") == "442079460958"


@pytest.mark.parametrize("raw", ["@Ivan_Petrov", "ivan_petrov", " IVAN_PETROV ",
                                 "https://t.me/ivan_petrov", "t.me/Ivan_Petrov?start=1"])
def test_username_key(raw):
    assert username_key(raw) == "ivan_petrov"


def test_one_number_in_different_spellings_is_one_phone():
    a = {"telegram_user_id": 1, "phones": '["+79991234567"]',
         "first_name": "Иван", "last_name": "Петров"}
    # второй — запись без Telegram ID (импорт, ручной ввод): два РАЗНЫХ
    # Telegram ID дублями не считаются вовсе (см. test_dedup_and_merge_keep_people_whole)
    b = {"telegram_user_id": None, "phones": '["8 (999) 123-45-67"]',
         "first_name": "Иван", "last_name": "Петров"}
    r = compute_merge_confidence(a, b)
    assert "same_phone" in r["reasons"], r
    assert r["confidence"] >= 0.5, r


def test_at_sign_and_case_do_not_make_two_people():
    a = {"telegram_user_id": 1, "username": "@Ivan_Petrov",
         "first_name": "Иван", "last_name": "Петров", "company": "acme"}
    # второй — запись без Telegram ID (импорт, ручной ввод): два РАЗНЫХ
    # Telegram ID дублями не считаются вовсе (см. test_dedup_and_merge_keep_people_whole)
    b = {"telegram_user_id": None, "username": "ivan_petrov",
         "first_name": "Иван", "last_name": "Петров", "company": "acme"}
    r = compute_merge_confidence(a, b)
    assert "same_username" in r["reasons"], r
    assert r["confidence"] >= 0.5, r


def test_different_numbers_still_differ():
    a = {"telegram_user_id": 1, "phones": ["+79991234567"],
         "first_name": "Иван", "last_name": "Петров"}
    # второй — запись без Telegram ID (импорт, ручной ввод): два РАЗНЫХ
    # Telegram ID дублями не считаются вовсе (см. test_dedup_and_merge_keep_people_whole)
    b = {"telegram_user_id": None, "phones": ["89991234568"],
         "first_name": "Иван", "last_name": "Петров"}
    assert "same_phone" not in compute_merge_confidence(a, b)["reasons"]


class _RowsPool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _contact(i, **kw):
    d = {"id": str(uuid.UUID(int=i)), "telegram_user_id": None, "username": None,
         "first_name": f"Имя{i}", "last_name": f"Фам{i}", "phones": [], "company": None}
    d.update(kw)
    return d


def test_the_screen_finds_a_twin_written_differently():
    """Группировка кандидатов обязана пользоваться ТЕМ ЖЕ ключом, что оценка:
    иначе пара с `+7…` и `8…` не встретится ни в одной группе."""
    rows = [
        _contact(1, phones='["+79991234567"]', first_name="Пётр", last_name="Смирнов"),
        _contact(2, phones='["8 999 123-45-67"]', first_name="Пётр", last_name="Смирнов"),
        _contact(3, username="@Anna", first_name="Анна", last_name="Ли", company="x"),
        _contact(4, username="anna", first_name="Анна", last_name="Ли", company="x"),
    ]
    res = _run(trust_engine.detect_smart_duplicates(_RowsPool(rows), 1))
    pairs = {tuple(sorted([d["contact_a"]["id"], d["contact_b"]["id"]]))
             for d in res["duplicates"]}
    assert pairs == {
        tuple(sorted([rows[0]["id"], rows[1]["id"]])),
        tuple(sorted([rows[2]["id"], rows[3]["id"]])),
    }, res


def test_merge_does_not_triple_one_number():
    primary = {"phones": '["+79991234567"]'}
    secondary = {"phones": '["89991234567", "79991234567", "+79990000000"]'}
    taken = absorbed_fields(primary, secondary)
    assert taken["phones"] == ["+79991234567", "+79990000000"], taken


def test_merge_phones_keeps_owner_spelling_and_order():
    assert merge_phones(["8 999 123-45-67"], ["+79991234567"]) == ["8 999 123-45-67"]
    assert merge_phones([], ["+79991234567"]) == ["+79991234567"]
    assert merge_phones(["+79991234567"], []) == ["+79991234567"]
    # Мусор не теряется — сравнивается строкой, как раньше.
    assert merge_phones(["доб. 12"], ["доб. 12", "доб. 13"]) == ["доб. 12", "доб. 13"]


# ── Синк не стирает телефоны (живой Postgres) ───────────────────────────────

from tests.test_contacts_sync_e2e_postgres import (  # noqa: E402,F401
    OWNER, _seed_account, _stub_contacts, pool,
)
from tests.test_contacts_sync_e2e_postgres import _run as _pg_run  # noqa: E402


def test_sync_keeps_phones_the_owner_added_postgres(pool, monkeypatch):
    from services.contacts_hub import sync_service

    acc_id = _pg_run(_seed_account(pool))
    # Первый синк: собеседник из диалогов, Telegram номера не отдаёт.
    dialog = [{"user_id": 55601, "username": "talker", "first_name": "Олег",
               "last_name": "", "phone": None}]
    _stub_contacts(monkeypatch, book=[], dialogs=dialog)
    res = _pg_run(sync_service.sync_account(pool, OWNER, acc_id))
    assert res.get("created") == 1, res

    # Владелец вписал номер вручную (или он пришёл слиянием дублей).
    _pg_run(pool.execute(
        "UPDATE unified_contacts SET phones='[\"8 916 000-11-22\"]'::jsonb "
        "WHERE owner_id=$1 AND telegram_user_id=55601", OWNER))

    # Повторный синк — Telegram по-прежнему номера не знает.
    res = _pg_run(sync_service.sync_account(pool, OWNER, acc_id))
    assert res.get("updated") == 1, res
    phones = _pg_run(pool.fetchval(
        "SELECT phones::text FROM unified_contacts "
        "WHERE owner_id=$1 AND telegram_user_id=55601", OWNER))
    assert "8 916 000-11-22" in phones, (
        f"синк стёр номер, который владелец внёс сам: {phones}")

    # Тот же номер из книги в другом написании не дублируется, новый — дописывается.
    book = [{"user_id": 55601, "username": "talker", "first_name": "Олег",
             "last_name": "", "phone": "79160001122"}]
    _stub_contacts(monkeypatch, book=book, dialogs=[])
    _pg_run(sync_service.sync_account(pool, OWNER, acc_id))
    phones = _pg_run(pool.fetchval(
        "SELECT phones::text FROM unified_contacts "
        "WHERE owner_id=$1 AND telegram_user_id=55601", OWNER))
    assert phones == '["8 916 000-11-22"]', phones


# ── Почта: тоже ключ опознания ──────────────────────────────────────────────
# НАХОДКА. Почта в сравнении дублей не участвовала вовсе: две записи одного
# клиента из CRM и импорта с одной почтой дублями не считались. Обратный
# контроль: убрать ('em', …) из группировки -> красный
# test_the_screen_finds_twins_by_email.

from services.contacts_hub.identity_keys import email_key  # noqa: E402


@pytest.mark.parametrize("raw", ["Ivan.Petrov+shop@gmail.com", " ivanpetrov@GMAIL.com ",
                                 "mailto:ivan.petrov@googlemail.com"])
def test_gmail_spellings_share_one_key(raw):
    assert email_key(raw) == "ivanpetrov@gmail.com"


def test_other_domains_keep_dots_and_plus():
    assert email_key("a.b+c@yandex.ru") == "a.b+c@yandex.ru"
    assert email_key("not-an-email") == ""
    assert email_key("x@localhost") == ""


def test_the_screen_finds_twins_by_email():
    rows = [
        _contact(10, emails='["Ivan.Petrov@gmail.com"]', first_name="Иван", last_name="Петров"),
        _contact(11, emails='["ivanpetrov+crm@gmail.com"]', first_name="Иван", last_name="Петров"),
    ]
    res = _run(trust_engine.detect_smart_duplicates(_RowsPool(rows), 1))
    assert len(res["duplicates"]) == 1, res
    assert "same_email" in res["duplicates"][0]["reasons"]
    assert "одна почта" in res["duplicates"][0]["reason_labels"]


def test_merge_does_not_double_one_mailbox():
    taken = absorbed_fields({"emails": '["Ivan.Petrov@gmail.com"]'},
                            {"emails": '["ivanpetrov@gmail.com", "i@ya.ru"]'})
    assert taken["emails"] == ["Ivan.Petrov@gmail.com", "i@ya.ru"]
