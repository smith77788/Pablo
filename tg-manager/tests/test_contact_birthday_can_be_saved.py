"""День рождения в карточке контакта сохраняется, а не роняет всю правку.

НАХОДКА (22.10.2026). `birthday` — это колонка DATE, и asyncpg биндит её БЕЗ
каста: строка вместо `datetime.date` роняет запрос целиком (DataError). А с
экрана приходит ровно строка — `<input type="date">.value`, то есть
«1990-05-01». Значит владелец, заполнивший день рождения, терял ВСЮ правку
карточки: имя, телефоны, заметки — всё уезжало в 500 «внутренняя ошибка», и
причину он узнать не мог.

Тот же дефект уже находили у соседнего поля `next_reminder_at` (см. комментарий
в `uch_crm_reminder`) — там его починили на границе обработчика, а про
день рождения забыли. Теперь приведение живёт в `update_contact`, то есть
одно на всех, кто правит карточку.

ОБРАТНЫЙ КОНТРОЛЬ (каждая проверка краснела на своей поломке): убрал
приведение из `update_contact` → «в запрос ушла не дата» и «пустое поле ушло
как ''»; убрал приведение у откатчика → «откат биндит дату из снимка сырой
строкой»; вернул откату запись дубля выбранной версии → «версия после отката
снова несёт дубль выбранной». Первая редакция последней проверки контроль НЕ
прошла: она искала слово `before` где-нибудь в вызове и зеленела от
соседнего `changed_fields=sorted(before)` — теперь аргумент снимка проверяется
на своём месте.
"""
from __future__ import annotations

import asyncio
import datetime

import pytest

from services.contacts_hub import repository as repo
from services.contacts_hub import versioning_engine as ve
from tests.miniapp_source import py_function


class _Pool:
    """Ловит то, что уходит в базу: сам SQL и связанные параметры."""

    def __init__(self, row=None):
        self.calls: list[tuple] = []
        self._row = row

    async def execute(self, sql, *params):
        self.calls.append((sql, params))
        return "UPDATE 1"

    async def fetchrow(self, sql, *params):
        return self._row

    async def fetchval(self, sql, *params):
        return 0

    async def acquire(self):
        raise AssertionError("версия в этих проверках не пишется")

    async def release(self, conn):
        pass


def test_coerce_date_understands_what_the_screen_sends():
    assert repo.coerce_date("1990-05-01") == datetime.date(1990, 5, 1)
    # Пустое поле — это «не указано», а не ошибка.
    assert repo.coerce_date("") is None
    assert repo.coerce_date(None) is None
    # Уже дата — как есть; datetime из базы — срезаем до даты.
    assert repo.coerce_date(datetime.date(2000, 1, 2)) == datetime.date(2000, 1, 2)
    assert repo.coerce_date(datetime.datetime(2000, 1, 2, 15, 30)) == datetime.date(2000, 1, 2)
    # ISO с временем (старые клиенты) тоже читается.
    assert repo.coerce_date("1990-05-01T00:00:00") == datetime.date(1990, 5, 1)


def test_an_unreadable_date_is_the_owners_error_not_a_server_fault():
    with pytest.raises(repo.BadDate):
        repo.coerce_date("не дата")
    # Сообщение читает владелец — оно обязано быть по-русски.
    try:
        repo.coerce_date("31.02.1990")
    except repo.BadDate as e:
        assert "дата" in str(e).lower(), f"владелец увидит «{e}»"


def test_the_date_reaches_the_database_as_a_date():
    """То, что уходит в asyncpg, обязано быть date — иначе падает вся правка."""
    pool = _Pool()
    ok = asyncio.run(repo.update_contact(
        pool, "c-1", 7, {"first_name": "Иван", "birthday": "1990-05-01"}))
    assert ok
    sql, params = pool.calls[-1]
    dates = [p for p in params if isinstance(p, datetime.date)]
    assert dates == [datetime.date(1990, 5, 1)], (
        f"в запрос ушла не дата: {params!r}")
    assert "1990-05-01" not in params, "ушла строка — asyncpg уронит запрос"


def test_an_empty_date_clears_the_field_instead_of_failing():
    """Стереть день рождения — законное действие, а не ошибка."""
    pool = _Pool()
    asyncio.run(repo.update_contact(pool, "c-1", 7, {"birthday": ""}))
    sql, params = pool.calls[-1]
    assert params[0] is None, f"пустое поле ушло как {params[0]!r}"


def test_the_handler_answers_400_and_not_500():
    """500 «внутренняя ошибка» заставляет жать «Сохранить» снова и снова."""
    handler = py_function("services/mini_app_uch.py", "uch_contact_update")
    assert "except BadDate" in handler, "непонятная дата снова выглядит сбоем сервера"
    i = handler.index("except BadDate")
    assert ", 400)" in handler[i:i + 400], "ошибка ввода отвечает не 400"
    # Внутри обработчика есть ещё один `except Exception` — вокруг записи
    # версии; сравниваем с ПОСЛЕДНИМ, то есть с перехватом самого обработчика.
    assert i < handler.rindex("except Exception"), (
        "общий перехват стоит раньше — до разбора ввода дело не дойдёт")


def test_one_field_list_for_the_whole_card():
    """Правка, снимок версии и откат обязаны знать один и тот же набор полей."""
    assert ve.RESTORABLE_FIELDS is repo.EDITABLE_FIELDS, (
        "список полей снова размножился: добавленное поле потеряется в откате")
    assert "EDITABLE_FIELDS" in py_function(
        "services/contacts_hub/repository.py", "update_contact"), (
        "правка карточки перебирает свой собственный список полей")
    assert len(repo.EDITABLE_FIELDS) >= 19, (
        f"полей стало {len(repo.EDITABLE_FIELDS)} — список усох")


def test_rollback_restores_the_date_as_a_date():
    """В снимке дата лежит строкой (json.dumps не умеет date) — значит откат
    обязан привести её обратно, иначе падает весь откат."""
    body = py_function("services/contacts_hub/versioning_engine.py",
                       "rollback_to_version")
    i = body.index("'birthday'")
    assert "coerce_date(" in body[i:i + 400], (
        "откат биндит дату из снимка сырой строкой — запрос упадёт целиком")


def test_rollback_keeps_what_it_overwrote():
    """Иначе сам откат необратим: ошибочное нажатие стирает карточку без возврата."""
    body = py_function("services/contacts_hub/versioning_engine.py",
                       "rollback_to_version")
    # Снимок текущего состояния снимается ДО перезаписи...
    assert body.index("snapshot_of(") < body.index("UPDATE unified_contacts"), (
        "состояние до отката не снимается — отменять откат будет нечем")
    # ...и именно он уходит в новую версию, а не дубль выбранной.
    # Именно снимок «до» уходит в версию аргументом снимка, а не выбранная
    # версия: проверка на «в вызове где-то упомянуто before» зеленела бы и от
    # соседнего `changed_fields=sorted(before)`.
    import re
    j = body.index("create_version(")
    seg = body[j:j + 300]
    assert re.search(r"create_version\(\s*pool,\s*contact_id,\s*owner_id,\s*before\b", seg), (
        "версия после отката снова несёт дубль выбранной, а не то, что затёрли")
    assert "Откат к v" in seg, "подпись версии не по-русски — её читает владелец"
