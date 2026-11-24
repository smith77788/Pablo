"""Импорт контактов из файла пачкой даёт ту же базу, что поштучный.

Поштучно файл в 10 000 строк шёл ~40 с (поиск по телефону и запись на
каждую строку); пачкой в одной транзакции — ~2,3 с, повторный файл ~3,9 с.
Тонкость — строки, которые сливаются друг с другом внутри файла и с уже
существующими контактами (в том числе по номеру, пришедшему слиянием).
Поэтому сверяем итоговые записи двух путей на одном и том же файле.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
A, B = 995501, 995502


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


SEED = [  # уже в базе
    {"phones": ["+79990000001"], "display_name": "Иван", "first_name": "Иван",
     "emails": ["i@x.ru"], "notes": "старый"},
    {"phones": ["+79990000002"], "display_name": "", "first_name": "", "company": "ООО"},
]
FILE = [
    {"phones": ["+79990000001"], "last_name": "Петров", "addresses": ["Москва"], "notes": "новый"},
    {"phones": ["+79990000002", "+79990000009"], "display_name": "Мария", "first_name": "Мария"},
    {"phones": ["+79990000009"], "emails": ["m@x.ru"]},            # по номеру из слияния
    {"phones": ["+79990000100"], "display_name": "Олег", "notes": ""},
    {"phones": ["+79990000100"], "company": "Рога", "notes": "звонил"},  # дубль внутри файла
    {"phones": [], "display_name": "Без номера"},
    {"phones": [], "display_name": "", "emails": []},              # пропуск
]


def _snapshot(rows):
    out = []
    for r in rows:
        d = dict(r)
        for k in ("phones", "emails", "addresses"):
            d[k] = json.loads(d[k]) if isinstance(d[k], str) else d[k]
        out.append(d)
    return sorted(out, key=lambda d: (d["phones"], d["display_name"] or ""))


def test_batched_equals_one_by_one():
    import asyncpg

    from services.contacts_hub import file_import as fi

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            res = {}
            for owner, path in ((A, "batch"), (B, "single")):
                await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", owner)
                await fi._import_one_by_one(pool, owner, SEED, "старт")
                if path == "batch":
                    summary = await fi.import_contacts(pool, owner, FILE, "файл")
                else:
                    summary = await fi._import_one_by_one(pool, owner, FILE, "файл")
                rows = await pool.fetch(
                    "SELECT first_name, last_name, patronymic, display_name, company, notes, "
                    "phones, emails, addresses, tags, persona_type FROM unified_contacts "
                    "WHERE owner_id=$1", owner)
                res[path] = (summary, _snapshot(rows))
            return res
        finally:
            for owner in (A, B):
                await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", owner)
            await pool.close()

    res = _run(_go())
    assert res["batch"][0] == res["single"][0]
    assert res["batch"][1] == res["single"][1]
    assert len(res["batch"][1]) == 4


def test_batch_path_is_taken(monkeypatch):
    """Поштучный запасной путь не должен молча подменять пачку."""
    import asyncpg

    from services.contacts_hub import file_import as fi

    async def _boom(*a, **kw):
        raise AssertionError("ушёл в поштучный путь")

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", A)
            monkeypatch.setattr(fi, "_import_one_by_one", _boom)
            return await fi.import_contacts(pool, A, FILE, "файл")
        finally:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", A)
            await pool.close()

    s = _run(_go())
    assert (s["created"], s["updated"], s["skipped"]) == (4, 2, 1), s


def test_lookup_is_one_query_and_survives_non_array_phones():
    """Поиск существующих — один запрос на весь файл, а не пачки `?|`, которые
    Postgres исполнял перебором (~4 с на 5000 номеров). Старая строка, где
    phones не массив, не должна ронять весь импорт в медленный поштучный путь."""
    import asyncpg

    from services.contacts_hub import file_import as fi

    async def body():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", A)
            await pool.execute(
                "INSERT INTO unified_contacts (owner_id, display_name, phones) "
                "VALUES ($1, 'старый', '\"+79990000077\"'::jsonb), "
                "       ($1, 'есть', '[\"+79990000001\"]'::jsonb)", A)
            calls = []
            orig = fi._import_one_by_one

            async def _slow(*a, **k):
                calls.append(1)
                return await orig(*a, **k)

            fi._import_one_by_one = _slow
            try:
                res = await fi.import_contacts(pool, A, [
                    {"phones": ["+79990000001"], "first_name": "Иван"},
                    {"phones": ["+79990000002"], "display_name": "Новый"}])
            finally:
                fi._import_one_by_one = orig
            assert not calls, "пачка упала и ушла в поштучный путь"
            assert (res["created"], res["updated"]) == (1, 1)
            assert fi._LOOKUP_SQL.count("FROM unified_contacts") == 1
            assert "?|" not in fi._LOOKUP_SQL
        finally:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", A)
            await pool.close()

    _run(body())


def test_import_planning_runs_off_the_event_loop():
    import inspect

    from services.contacts_hub import file_import as fi
    src = inspect.getsource(fi._import_batched)
    assert "asyncio.to_thread(" in src and "_plan_import" in src
