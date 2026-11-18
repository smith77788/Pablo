"""Импорт базы клиентов из файла в хранилище контактов.

Просьба владельца (18.11.2026): «я до сих пор не могу загрузить в хранилище
контактов документы с базами номеров своих клиентов… Документы должны быть
грамотно расшифрованы ведь в них указаны и имена и адреса и номера и всякая
другая разная информация и добавлены в хранилище с соответствующими записями
имени и тд».

Проверяем: разбор форматов (CSV с рус/англ заголовками, без заголовков, Excel,
vCard, свободный текст), нормализацию телефона, дедуп ПО ТЕЛЕФОНУ при записи
(повтор — слияние, а не дубль), и что ручка /uch/import зарегистрирована и
предлагает синк флота.
"""
from __future__ import annotations

import asyncio
import io
import os

from services.contacts_hub import file_import as fi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Нормализация телефона ────────────────────────────────────────────────────

def test_norm_phone_variants():
    assert fi.norm_phone("+7 (999) 123-45-67") == "+79991234567"
    assert fi.norm_phone("8 999 123 45 67") == "+79991234567"  # 8 → +7
    assert fi.norm_phone("89991234567") == "+79991234567"
    assert fi.norm_phone("abc") == ""
    assert fi.norm_phone("12345") == ""  # слишком коротко
    assert fi.norm_phone("") == ""


# ── Разбор форматов ──────────────────────────────────────────────────────────

def test_csv_with_russian_headers():
    data = ("Имя;Фамилия;Телефон;Почта;Адрес;Компания\n"
            "Иван;Петров;+7 999 111-22-33;ivan@mail.ru;Москва;ООО Ромашка\n"
            "Пётр;Сидоров;89995556677;;;\n").encode("utf-8")
    rows = fi.parse_contacts("base.csv", data)
    assert len(rows) == 2
    r = rows[0]
    assert r["first_name"] == "Иван" and r["last_name"] == "Петров"
    assert r["phones"] == ["+79991112233"]
    assert r["emails"] == ["ivan@mail.ru"]
    assert "Москва" in r["addresses"] and r["company"] == "ООО Ромашка"
    assert rows[1]["phones"] == ["+79995556677"]


def test_csv_english_headers_and_comma():
    data = ("name,phone,email\n"
            "John Smith,+1 202 555 0100,john@example.com\n").encode("utf-8")
    rows = fi.parse_contacts("c.csv", data)
    assert len(rows) == 1
    assert rows[0]["phones"] == ["+12025550100"]
    assert rows[0]["display_name"] == "John Smith"


def test_csv_without_headers_detects_phone_column():
    data = ("Иван Петров;+79991112233\nМария;+79992223344\n").encode("utf-8")
    rows = fi.parse_contacts("noheader.csv", data)
    assert len(rows) == 2
    assert rows[0]["phones"] == ["+79991112233"]
    assert "Иван" in rows[0]["display_name"]


def test_plain_text_with_numbers():
    data = ("Иван Петров +7 999 111 22 33\nкакой-то мусор без номера\n"
            "Мария 8 999 222 33 44\n").encode("utf-8")
    rows = fi.parse_contacts("list.txt", data)
    assert len(rows) == 2
    assert rows[0]["phones"] == ["+79991112233"]
    assert rows[1]["phones"] == ["+79992223344"]


def test_vcard():
    data = ("BEGIN:VCARD\nVERSION:3.0\nFN:Иван Петров\nN:Петров;Иван;;;\n"
            "TEL;TYPE=CELL:+7 999 111 22 33\nEMAIL:ivan@mail.ru\n"
            "ORG:ООО Ромашка\nEND:VCARD\n").encode("utf-8")
    rows = fi.parse_contacts("c.vcf", data)
    assert len(rows) == 1
    r = rows[0]
    assert r["phones"] == ["+79991112233"]
    assert r["last_name"] == "Петров" and r["first_name"] == "Иван"
    assert r["emails"] == ["ivan@mail.ru"] and r["company"] == "ООО Ромашка"


def test_xlsx_roundtrip():
    try:
        from openpyxl import Workbook
    except Exception:
        return  # без openpyxl ветка xlsx недоступна — не наша забота тут
    wb = Workbook()
    ws = wb.active
    ws.append(["Имя", "Телефон"])
    ws.append(["Иван", "+79991112233"])
    ws.append(["Мария", "89992223344"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = fi.parse_contacts("base.xlsx", buf.getvalue())
    assert len(rows) == 2
    assert rows[0]["phones"] == ["+79991112233"]
    assert rows[1]["phones"] == ["+79992223344"]


def test_empty_file_gives_no_rows():
    assert fi.parse_contacts("x.csv", b"") == []
    assert fi.parse_contacts("x.txt", b"\n\n   \n") == []


# ── Ручка и фронт ────────────────────────────────────────────────────────────

def test_route_registered():
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "POST /api/miniapp/uch/import" in snap


def test_endpoint_queues_background_import():
    """Эндпоинт НЕ разбирает инлайн (большая база не укладывается в таймаут):
    сохраняет файл во временный и ставит фоновую операцию contacts_import."""
    src = _src("services/mini_app_uch.py")
    body = src[src.index("async def uch_contacts_import("):]
    body = body[:body.index("async def uch_sync_center(")]
    assert "read_upload(field, _UCH_IMPORT_MAX)" in body
    assert "mkstemp" in body, "файл сохраняется во временный для воркера"
    assert '"contacts_import"' in body and "operation_bus.submit(" in body
    assert '"queued": True' in body
    # Разбор/запись НЕ в запросе — они в воркере.
    assert "import_contacts(" not in body


def test_worker_executor_parses_imports_and_chains_push():
    src = _src("services/op_worker.py")
    body = src[src.index("async def _exec_contacts_import("):]
    body = body[:body.index("async def _exec_contacts_push(")]
    assert "parse_contacts(" in body and "import_contacts(" in body
    # По успеху — авто-выгрузка в книги флота (contacts_push).
    assert '"contacts_push"' in body
    # Временный файл удаляется.
    assert "unlink(path)" in body
    # Зарегистрирован в диспетчере op.
    assert '"contacts_import": _dispatch_contacts_import' in src


def test_frontend_import_is_background():
    html = _src("mini_app/index.html")
    assert "importContactsFile()" in html
    assert "async function _doImportContacts(" in html
    assert "/api/miniapp/uch/import" in html
    fn = html[html.index("async function _doImportContacts("):]
    fn = fn[:fn.index("\nasync function ", 1)] if "\nasync function " in fn[10:] else fn[:2000]
    # Фоновый ответ: queued, подтягиваем список по мере обработки.
    assert "d.queued" in fn and "loadContacts()" in fn


# ── Живая БД: дедуп по телефону (слияние, не дубль) ───────────────────────────
# Нужна настоящая база: заглушка пула не проверяет типы и не исполняет SQL —
# ошибка связывания jsonb/`?|` на юнит-тестах невидима в принципе.

import asyncio  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990501

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


def test_import_dedups_by_phone_on_live_db(pool):
    async def _go():
        await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)

        rows1 = fi.parse_contacts("a.csv", (
            "Имя;Телефон;Почта\nИван;+7 999 111 22 33;ivan@mail.ru\n"
            "Мария;+7 999 222 33 44;\n").encode("utf-8"))
        s1 = await fi.import_contacts(pool, OWNER, rows1, source="импорт")
        assert s1["created"] == 2
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", OWNER) == 2

        # Второй файл: тот же Иван по телефону (+ фамилия и адрес) и новый контакт.
        rows2 = fi.parse_contacts("b.csv", (
            "Имя;Фамилия;Телефон;Адрес\nИван;Петров;8 999 111 22 33;Москва\n"
            "Олег;;+7 999 777 88 99;\n").encode("utf-8"))
        s2 = await fi.import_contacts(pool, OWNER, rows2, source="импорт")
        assert s2["created"] == 1, "новый контакт должен добавиться"
        assert s2["updated"] == 1, "совпавший по телефону — слиться, не задвоиться"
        assert await pool.fetchval(
            "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", OWNER) == 3, (
            "дубля по телефону быть не должно")

        merged = await pool.fetchrow(
            "SELECT last_name, addresses, emails FROM unified_contacts "
            "WHERE owner_id=$1 AND phones ?| ARRAY['+79991112233'] LIMIT 1", OWNER)
        assert merged["last_name"] == "Петров", "пустое поле дополнилось при слиянии"
        assert "Москва" in json.loads(merged["addresses"])
        assert "ivan@mail.ru" in json.loads(merged["emails"]), "почта из 1-го файла цела"

        await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)

    _run(_go())
