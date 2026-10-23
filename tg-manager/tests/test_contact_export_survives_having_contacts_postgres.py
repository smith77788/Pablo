"""Выгрузка контактов работает, когда контакты ЕСТЬ. JSON не работал ни разу.

ЧТО БЫЛО. `export_json` приводила к строке пять колонок с временем, перечислив
их по именам, и отдавала результат в `json.dumps` без общего сериализатора.
Но запрос — `SELECT *`, а у `unified_contacts.id` тип UUID, и его в том списке
не было: `json.dumps` падал «Object of type UUID is not JSON serializable».

То есть кнопка «выгрузить контакты в JSON» (`/api/miniapp/uch/export/json`) не
работала НИ РАЗУ у владельца, у которого есть контакты: на пустом справочнике
возвращался «[]», а на непустом — «внутренняя ошибка сервера».

ПОЧЕМУ НЕ ПОЙМАЛИ. Скан всех 284 GET-ручек (см.
tests/test_every_screen_survives_an_empty_account_postgres.py) ходит владельцем
БЕЗ данных — ровно тот случай, когда выгрузка отвечает «[]» и выглядит
исправной. Класс «падает, только когда данные есть» нужно проверять владельцем
с данными, и этот файл — такая проверка для выгрузки.

Перечисление колонок по именам и не могло держаться: в таблице 60+ колонок, и
следующая колонка с датой или uuid снова сломала бы выгрузку. Поэтому фикс —
общий сериализатор (`default=str`), как в `_json_resp`.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

os.environ.setdefault("DATABASE_URL", DSN or "postgresql://localhost/none")

OWNER = 776655443403

_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro):
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
        await p.execute(
            "INSERT INTO unified_contacts(id, owner_id, telegram_user_id, username,"
            " first_name, last_name, company, tags, display_name, phones)"
            " VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            uuid.uuid4(), OWNER, 3001, "exportme", "Иван", "Петров", "ООО Ромашка",
            ["vip"], "Иван Петров", '["+79990000000"]')
        return p

    p = _run(_mk())
    yield p
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.close())
    global _LOOP
    if _LOOP is not None:
        _LOOP.close()
        _LOOP = None


def test_json_export_returns_the_contact(pool):
    from services.contacts_hub.export_engine import export_json

    data = json.loads(_run(export_json(pool, OWNER)))
    assert len(data) == 1, f"в выгрузке {len(data)} контактов вместо одного"
    row = data[0]
    assert row["username"] == "exportme"
    # id — uuid, и именно он ронял сериализацию
    assert uuid.UUID(row["id"]), "id контакта не попал в выгрузку"
    assert row["discovered_at"], "дата в выгрузке пустая"


def test_the_json_export_is_valid_json_for_every_column(pool):
    """Антивакуумность: проверяем не «не упало», а что JSON разбирается целиком.

    Любая новая колонка с датой, uuid, Decimal или jsonb обязана пройти через
    общий сериализатор, а не через список имён.
    """
    raw = _run(__import__(
        "services.contacts_hub.export_engine", fromlist=["x"]).export_json(pool, OWNER))
    parsed = json.loads(raw)
    assert isinstance(parsed, list) and parsed
    assert json.dumps(parsed), "разобранная выгрузка не сериализуется обратно"


def test_the_other_exports_work_too(pool):
    """CSV и vCard идут из той же таблицы — их тоже проверяем с данными."""
    from services.contacts_hub.export_engine import export_csv, export_vcf

    csv_text = _run(export_csv(pool, OWNER))
    assert "exportme" in csv_text
    vcf = _run(export_vcf(pool, OWNER))
    assert "BEGIN:VCARD" in vcf and "Иван" in vcf
    # (get_export_stats отсюда убран вместе с кодом: его не звала ни одна
    # ручка и ни один экран, а числа о контактах владелец видит в /uch/stats.)


def test_exporting_a_chosen_few_really_filters(pool):
    """Выгрузка СПИСКА id — это «выгрузить отмеченных» и «визитка» в карточке.

    Проверка именно на живой базе: id контакта — uuid, а в запрос он уходит
    строкой. asyncpg биндит uuid без каста, и на заглушке такая выгрузка
    выглядела бы работающей, пока Postgres отбивал бы её целиком — ровно так
    уже обожглись на пересчёте доверия.
    """
    import json as _json

    from services.contacts_hub.export_engine import export_json, export_vcf

    second = _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name, username) "
        "VALUES ($1,'Пётр','petr') RETURNING id", OWNER))
    only = _run(export_json(pool, OWNER, [str(second)]))
    rows = _json.loads(only)
    assert len(rows) == 1, "в выгрузку попали не только выбранные"
    assert rows[0]["username"] == "petr"
    card = _run(export_vcf(pool, OWNER, [str(second)]))
    assert card.count("BEGIN:VCARD") == 1, "визитка одного контакта — не одна"


def test_a_stranger_cannot_export_my_contact_by_id(pool):
    """id знать мало: выгрузка ограничена владельцем в самом запросе."""
    import json as _json

    from services.contacts_hub.export_engine import export_json

    mine = _run(pool.fetchval(
        "SELECT id FROM unified_contacts WHERE owner_id=$1 LIMIT 1", OWNER))
    assert mine is not None
    stolen = _run(export_json(pool, OWNER + 1, [str(mine)]))
    assert _json.loads(stolen) == [], "чужой контакт выгрузился по id"
