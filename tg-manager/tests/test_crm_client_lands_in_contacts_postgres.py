"""Клиент из «CRM → Клиенты» сразу виден в «Контактах» (шаг 1 сведения баз).

Две базы людей жили порознь: человек, заведённый во вкладке «Клиенты»
(`crm_contacts`), не попадал ни в «Контакты», ни в сделки, ни в рассылки по
контактам. Владелец решил свести их в одну (08.11.2026). Первый шаг — каждая
запись в старую таблицу зеркалится в `unified_contacts` по правилам из
`services/contacts_hub/legacy_crm.py`. Здесь стережём сами правила: повтор не
плодит дублей, заполненное не затирается, придуманный ID не выдаётся за
настоящий, исключение из рассылок не снимается.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest

from services.contacts_hub.legacy_crm import mirror_crm_contact

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993_811
ROOT = pathlib.Path(__file__).resolve().parents[1]
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def test_both_write_paths_mirror_into_contacts():
    """Без Postgres: обе двери записи в crm_contacts зовут зеркало."""
    for rel in ("services/mini_app_broadcast.py", "bot/handlers/crm.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert src.count("INSERT INTO crm_contacts") == 1, rel
        tail = src[src.index("INSERT INTO crm_contacts"):]
        assert "await mirror_crm_contact(" in tail[:2500], (
            f"{rel}: клиент пишется в старую базу, но не появляется в «Контактах»")
    assert "tg_is_real=False" in (ROOT / "bot/handlers/crm.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    yield p
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.close())


def _rows(pool):
    return _run(pool.fetch(
        "SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY created_at", OWNER))


def test_new_client_appears_once_and_repeat_does_not_duplicate(pool):
    for _ in range(2):
        cid = _run(mirror_crm_contact(
            pool, OWNER, legacy_id=101, tg_user_id=5_555_000_001, tg_is_real=False,
            username="@ivan_new", first_name="Иван", phone="+79990000001",
            tags=["vip"], notes="звонить утром"))
        assert cid
    rows = [r for r in _rows(pool) if r["username"] == "ivan_new"]
    assert len(rows) == 1, "повторная запись завела второго человека"
    r = rows[0]
    assert r["telegram_user_id"] is None, "придуманный ID записан как настоящий"
    cf = json.loads(r["custom_fields"]) if isinstance(r["custom_fields"], str) else r["custom_fields"]
    assert cf["legacy_crm_id"] == 101 and cf["legacy_crm_created"] is True
    assert cf["legacy_tg_user_id"] == 5_555_000_001
    phones = json.loads(r["phones"]) if isinstance(r["phones"], str) else r["phones"]
    assert phones == ["+79990000001"] and r["tags"] == ["vip"]
    assert r["notes"] == "звонить утром"


def test_existing_contact_is_filled_not_overwritten(pool):
    _run(pool.execute(
        """INSERT INTO unified_contacts (owner_id, telegram_user_id, username,
             first_name, notes, tags, phones, excluded)
           VALUES ($1, 7_000_000_002, 'anna', 'Анна', 'старая заметка',
                   ARRAY['client'], '["+70000000002"]'::jsonb, TRUE)""", OWNER))
    cid = _run(mirror_crm_contact(
        pool, OWNER, legacy_id=202, tg_user_id=7_000_000_002, tg_is_real=True,
        username="other_name", first_name="Аня", last_name="Петрова",
        phone="+70000000003", tags=["vip", "client"], notes="новая заметка"))
    assert cid
    rows = [r for r in _rows(pool) if r["telegram_user_id"] == 7_000_000_002]
    assert len(rows) == 1
    r = rows[0]
    assert r["first_name"] == "Анна" and r["username"] == "anna", "заполненное затёрто"
    assert r["last_name"] == "Петрова", "пустое поле не дополнено"
    assert sorted(r["tags"]) == ["client", "vip"]
    assert r["notes"] == "старая заметка\nновая заметка"
    phones = json.loads(r["phones"]) if isinstance(r["phones"], str) else r["phones"]
    assert phones == ["+70000000002", "+70000000003"]
    assert r["excluded"] is True, "слияние вернуло человека в рассылки"
    cf = json.loads(r["custom_fields"]) if isinstance(r["custom_fields"], str) else r["custom_fields"]
    assert cf["legacy_crm_id"] == 202 and "legacy_crm_created" not in cf


def test_match_by_username_and_phone_when_no_real_id(pool):
    _run(pool.execute(
        "INSERT INTO unified_contacts (owner_id, username) VALUES ($1, 'Petr_X')", OWNER))
    _run(pool.execute(
        "INSERT INTO unified_contacts (owner_id, phones) VALUES ($1, '[\"+71112223344\"]'::jsonb)",
        OWNER))
    _run(mirror_crm_contact(pool, OWNER, legacy_id=303, tg_user_id=3_000_000_303,
                            username="petr_x"))
    _run(mirror_crm_contact(pool, OWNER, legacy_id=304, phone="+71112223344",
                            first_name="Телефон"))
    by_user = [r for r in _rows(pool) if (r["username"] or "").lower() == "petr_x"]
    by_phone = [r for r in _rows(pool) if r["first_name"] == "Телефон"]
    assert len(by_user) == 1 and len(by_phone) == 1


def test_failure_does_not_raise():
    class Broken:
        def acquire(self):
            raise RuntimeError("база упала")
    assert _run(mirror_crm_contact(Broken(), OWNER, legacy_id=1, first_name="x")) is None


def test_backfill_is_idempotent_and_dry_run_changes_nothing(pool):
    """Шаг 2: перенос старых строк. Пробный прогон считает и откатывает,
    настоящий переносит, повтор ничего не плодит, старая таблица цела."""
    from services.contacts_hub.legacy_crm import backfill_legacy_crm
    _run(pool.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(pool.execute(
        """INSERT INTO crm_contacts (owner_id, tg_user_id, username, first_name, tags, notes, source)
           VALUES ($1, 4_100_000_001, 'olga', 'Ольга', ARRAY['vip'], 'з1', 'manual'),
                  ($1, 4_100_000_002, NULL, 'Пётр', '{}', NULL, 'parsed'),
                  ($1, 4_100_000_003, 'OLGA', 'Оля', ARRAY['new'], 'з2', 'manual')""",
        OWNER))
    before = _run(pool.fetchval("SELECT COUNT(*) FROM unified_contacts"))
    dry = _run(backfill_legacy_crm(pool, dry_run=True))
    assert _run(pool.fetchval("SELECT COUNT(*) FROM unified_contacts")) == before
    mine = {k: v for k, v in dry.items()}
    assert mine["failed"] == 0 and mine["total"] >= 3
    real = _run(backfill_legacy_crm(pool))
    assert real["created"] == dry["created"] and real["matched"] == dry["matched"]
    rows = _rows(pool)
    assert len(rows) == 2, "«olga» и «OLGA» — один человек"
    olga = next(r for r in rows if (r["username"] or "").lower() == "olga")
    assert sorted(olga["tags"]) == ["new", "vip"] and olga["telegram_user_id"] is None
    petr = next(r for r in rows if r["first_name"] == "Пётр")
    assert petr["telegram_user_id"] == 4_100_000_002, "ID из парсинга — настоящий"
    again = _run(backfill_legacy_crm(pool))
    assert again["created"] == 0
    assert len(_rows(pool)) == 2
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1", OWNER)) == 3
    _run(pool.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER))


def test_startup_backfill_runs_once_and_is_wired():
    """Перенос подключён к старту веб-процесса и отмечается в журнале."""
    src = (ROOT / "services/mini_app_api.py").read_text(encoding="utf-8")
    hook = src[src.index("async def _apply_inline_migrations(application"):][:900]
    assert "run_backfill_once(pool)" in hook


def test_run_backfill_once_marks_journal(pool):
    from services.contacts_hub.legacy_crm import BACKFILL_KEY, run_backfill_once
    # Журнал заводит раннер миграций (database/db.py), а не schema_v*.sql.
    _run(pool.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY,
             status TEXT NOT NULL, error_count INT NOT NULL DEFAULT 0,
             last_error TEXT, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    _run(pool.execute("DELETE FROM schema_migrations WHERE filename=$1", BACKFILL_KEY))
    first = _run(run_backfill_once(pool))
    assert first is not None and first["failed"] == 0
    assert _run(pool.fetchval(
        "SELECT status FROM schema_migrations WHERE filename=$1", BACKFILL_KEY)) == "ok"
    assert _run(run_backfill_once(pool)) is None, "перенос пошёл второй раз"
    _run(pool.execute("DELETE FROM schema_migrations WHERE filename=$1", BACKFILL_KEY))
