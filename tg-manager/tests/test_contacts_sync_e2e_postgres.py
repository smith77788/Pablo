"""Контакт-синк по НАСТОЯЩЕМУ Postgres. Заглушён только Telethon.

ЗАЧЕМ ОТДЕЛЬНО. Заглушка пула в conftest НЕ проверяет типы параметров: она
принимает что угодно, поэтому ошибки СВЯЗЫВАНИЯ (ISO-строка даты вместо
`datetime.date`) на юнит-тестах невидимы В ПРИНЦИПЕ. Именно так пережил релиз
`registered_estimate=$9::date` со строковым параметром: asyncpg выводит тип
параметра из запроса, кодирует строку как `date`, зовёт у неё `.toordinal()` и
падает `'str' object has no attribute 'toordinal'` — на КАЖДОМ контакте, т.е.
синк давал 0 при тысячах контактов. Фикс — `$N::text::date` /
`$N::text::timestamptz`: приведение text→тип уже внутри Postgres. Свод, класс 23.

Этот файл фиксирует фикс регрессом, который падает БЕЗ него (голый `::date` с
ISO-строкой) и проходит С ним (реальный upsert через sync_account).

КАК ЗАПУСТИТЬ — см. docstring tests/test_invite_e2e_postgres.py (тот же стенд):
    export INFRAGRAM_TEST_DSN="postgresql://postgres@/infra?host=$D/sock&port=55432"
    pytest tests/test_contacts_sync_e2e_postgres.py -v
Без переменной окружения файл пропускается.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER = 990777

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
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
                pass  # схемы идемпотентны; частичный сбой не рушит прогон
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


async def _seed_account(p) -> int:
    # Полная очистка стенда владельца — тесты самодостаточны между прогонами
    # (Postgres переживает сессию pytest; иначе INSERT-ветка превращается в UPDATE).
    await p.execute(
        "DELETE FROM contact_sources WHERE contact_id IN "
        "(SELECT id FROM unified_contacts WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    return await p.fetchval(
        "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name) "
        "VALUES($1,'+79907770001','sess-cs',TRUE,'active','КонтактСинк') RETURNING id",
        OWNER)


def _stub_contacts(monkeypatch, book, dialogs):
    """Подменить telethon-вызовы account_manager на фиксированные данные."""
    import services.account_manager as am

    async def _get_contacts(session, _acc=None):
        return list(book)

    async def _get_dialog_contacts(session, limit=500, _acc=None):
        return list(dialogs)

    monkeypatch.setattr(am, "get_contacts", _get_contacts)
    monkeypatch.setattr(am, "get_dialog_contacts", _get_dialog_contacts)


# ── регресс: ISO-строки даты/времени связываются без .toordinal()-краха ──────

def test_bare_date_cast_crashes_on_iso_string(pool):
    """Documents fails-without-fix: голый ::date с ISO-строкой падает на живом драйвере.

    Это ровно то, что делал прежний запрос синка. ::text::date (фикс) — не падает.
    """
    import asyncpg

    async def _check():
        async with pool.acquire() as c:
            with pytest.raises(asyncpg.DataError):
                await c.fetchval("SELECT $1::date", "2020-01-15")
            # Фикс: приведение внутри Postgres — параметр остаётся text.
            assert str(await c.fetchval("SELECT $1::text::date", "2020-01-15")) == "2020-01-15"
    _run(_check())


def test_sync_account_upserts_iso_dates_and_jsonb(pool, monkeypatch):
    """sync_account кладёт контакт с ISO-строками reg_est/last_seen и jsonb-полями
    БЕЗ краша связывания — доказательство фикса на реальном upsert."""
    book = [{
        "user_id": 55501, "username": "iso_user", "first_name": "Иван",
        "last_name": "Дат", "phone": "+79001112233", "is_premium": True,
        "is_mutual": True, "is_verified": False,
        "registered_estimate": "2019-06-01",              # ISO-строка даты
        "last_seen_type": "recently",
        "last_seen_at": "2026-07-30T12:34:56+00:00",       # ISO-строка времени
        "access_hash": 7477083978437332073,
    }]
    _stub_contacts(monkeypatch, book, dialogs=[])
    acc_id = _run(_seed_account(pool))

    from services.contacts_hub import sync_service
    res = _run(sync_service.sync_account(pool, OWNER, acc_id))

    assert not res.get("error"), f"синк упал: {res.get('error')}"
    assert res.get("synced") == 1 and res.get("created") == 1

    row = _run(pool.fetchrow(
        "SELECT registered_estimate, last_seen_at, phones, digital_footprint, is_mutual "
        "FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2", OWNER, 55501))
    assert row is not None, "контакт не сохранился"
    # Даты реально распарсились в date/timestamptz, а не остались мусором.
    assert str(row["registered_estimate"]) == "2019-06-01"
    assert row["last_seen_at"] is not None
    assert row["is_mutual"] is True
    # jsonb-поля сохранены (phones — список с телефоном; footprint содержит access_hash).
    import json as _json
    phones = row["phones"] if isinstance(row["phones"], list) else _json.loads(row["phones"])
    assert phones == ["+79001112233"]
    fp = row["digital_footprint"]
    fp = fp if isinstance(fp, dict) else _json.loads(fp)
    assert str(fp.get("access_hash")) == "7477083978437332073"


def test_sync_account_handles_null_dates(pool, monkeypatch):
    """reg_est/last_seen = None (частый случай) не должны ломать ::text::date-каст."""
    book = [{
        "user_id": 55502, "username": "nulldate", "first_name": "Без",
        "last_name": "Даты", "is_mutual": False,
        "registered_estimate": None, "last_seen_type": None, "last_seen_at": None,
    }]
    _stub_contacts(monkeypatch, book, dialogs=[])
    acc_id = _run(_seed_account(pool))

    from services.contacts_hub import sync_service
    res = _run(sync_service.sync_account(pool, OWNER, acc_id))
    assert not res.get("error"), f"синк упал на None-датах: {res.get('error')}"

    row = _run(pool.fetchrow(
        "SELECT registered_estimate, last_seen_at FROM unified_contacts "
        "WHERE owner_id=$1 AND telegram_user_id=$2", OWNER, 55502))
    assert row is not None and row["registered_estimate"] is None and row["last_seen_at"] is None


def test_sync_account_update_path_rebinds_dates(pool, monkeypatch):
    """Повторный синк того же контакта идёт по UPDATE-ветке (тоже ::text::date) —
    её связывание проверяем отдельно от INSERT."""
    base = {
        "user_id": 55503, "username": "upd", "first_name": "Апдейт",
        "is_mutual": False, "registered_estimate": "2018-01-01",
        "last_seen_type": "recently", "last_seen_at": "2026-01-01T00:00:00+00:00",
    }
    acc_id = _run(_seed_account(pool))
    from services.contacts_hub import sync_service

    _stub_contacts(monkeypatch, [dict(base)], dialogs=[])
    _run(sync_service.sync_account(pool, OWNER, acc_id))

    # Второй прогон: новые дата/время → должна отработать UPDATE-ветка без краша.
    upd = dict(base, registered_estimate="2020-12-31",
               last_seen_at="2026-07-31T23:59:59+00:00", is_mutual=True)
    _stub_contacts(monkeypatch, [upd], dialogs=[])
    res = _run(sync_service.sync_account(pool, OWNER, acc_id))
    assert not res.get("error") and res.get("updated") == 1

    row = _run(pool.fetchrow(
        "SELECT registered_estimate, is_mutual FROM unified_contacts "
        "WHERE owner_id=$1 AND telegram_user_id=$2", OWNER, 55503))
    assert str(row["registered_estimate"]) == "2020-12-31" and row["is_mutual"] is True


# ── полный пользовательский путь: очередь-операция «синхронизация» ────────────

def _seed_accounts(p, n) -> list[int]:
    async def _s():
        await p.execute(
            "DELETE FROM contact_sources WHERE contact_id IN "
            "(SELECT id FROM unified_contacts WHERE owner_id=$1)", OWNER)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        ids = []
        for i in range(n):
            ids.append(await p.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name) "
                "VALUES($1,$2,$3,TRUE,'active',$4) RETURNING id",
                OWNER, f"+7991{i:07d}", f"cs{i}", f"CS{i}"))
        return ids
    return _run(_s())


def test_contacts_sync_operation_end_to_end(pool, monkeypatch):
    """Путь как у пользователя: operation_bus.submit('contacts_sync') → воркер →
    контакты флота в хабе + честный итог операции. Ловит и связывание в submit
    (историю с scheduled_for), и диспетчеризацию исполнителя, и запись контактов.

    Per-account данные разные — проверяем, что sync проходит ВЕСЬ флот, а не один.
    """
    import services.account_manager as am

    async def _gc(session, _acc=None):
        base = int(_acc["id"]) * 1000 if _acc else 0
        return [{"user_id": base + j, "first_name": f"K{j}",
                 "phone": f"+7{j:09d}" if j % 2 else None, "is_mutual": j == 0,
                 "registered_estimate": "2020-05-05", "last_seen_type": "recently",
                 "last_seen_at": "2026-07-01T00:00:00+00:00",
                 "access_hash": 123456789012345 + j} for j in range(3)]

    async def _gdc(session, limit=500, _acc=None):
        return []

    monkeypatch.setattr(am, "get_contacts", _gc)
    monkeypatch.setattr(am, "get_dialog_contacts", _gdc)

    ids = _seed_accounts(pool, 2)

    from services import operation_bus, op_worker as w

    async def _flow():
        op_id = await operation_bus.submit(pool, OWNER, "contacts_sync", {},
                                           total_items=len(ids), label="e2e sync")
        # Поллер помечает running и передаёт задачу воркеру — воспроизводим 1:1.
        await pool.execute("UPDATE operation_queue SET status='running', started_at=now() "
                           "WHERE id=$1", op_id)
        row = await pool.fetchrow(
            "SELECT id, owner_id, op_type, params FROM operation_queue WHERE id=$1", op_id)
        await w._run_op_task(pool, None, dict(row))
        return await pool.fetchrow(
            "SELECT status, result->>'summary' AS summary FROM operation_queue WHERE id=$1", op_id)

    final = _run(_flow())
    assert final["status"] == "done", f"операция не завершилась: {final['status']}"
    # 2 аккаунта × 3 контакта = 6, все разные user_id → флот пройден целиком.
    assert "Синхронизировано" in (final["summary"] or ""), final["summary"]
    cnt = _run(pool.fetchval("SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", OWNER))
    assert cnt == 6, f"ожидалось 6 контактов флота, в хабе {cnt}"
    # Каждый контакт привязан к своему аккаунту-источнику (счёт «по аккаунтам»).
    src = _run(pool.fetchval(
        "SELECT COUNT(*) FROM contact_sources WHERE account_id=ANY($1::bigint[])", ids))
    assert src == 6, f"источники по аккаунтам не записаны: {src}"
