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


def test_both_write_paths_go_straight_to_contacts():
    """Без Postgres: обе двери записи «Клиентов» (форма мини-аппа и бот) пишут
    в «Контакты» через add_crm_client, а старую таблицу не пополняет никто
    (шаг 5). Новая запись в crm_contacts снова развела бы две базы."""
    import re
    for rel in ("services/mini_app_broadcast.py", "bot/handlers/crm.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "await add_crm_client(" in src, rel
    for path in list((ROOT / "services").rglob("*.py")) + list((ROOT / "bot").rglob("*.py")):
        assert not re.search(r"INSERT\s+INTO\s+crm_contacts\b",
                             path.read_text(encoding="utf-8")), path


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


def _call(pool, method, path, body=None):
    """Позвать ручку /crm/* настоящим HTTP-запросом от имени OWNER."""
    async def go():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from services import mini_app_api as M
        from services import mini_app_broadcast as B
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        try:
            app = web.Application()
            B.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.request(method, path, json=body)
                return resp.status, await resp.json()
            finally:
                await client.close()
        finally:
            M._get_uid = orig
    return _run(go())


def test_clients_tab_shows_contacts_and_deletes_from_both(pool):
    """Шаг 3: вкладка «Клиенты» — это «Контакты». Человек, заведённый в
    «Контактах», виден во вкладке; клиент, добавленный во вкладке, виден в
    «Контактах»; удаление убирает его из обеих таблиц."""
    _run(pool.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(pool.execute(
        "INSERT INTO unified_contacts (owner_id, username, first_name, tags) "
        "VALUES ($1, 'from_hub', 'Из контактов', ARRAY['hub'])", OWNER))
    st, d = _call(pool, "POST", "/api/miniapp/crm/contact",
                  {"first_name": "Из вкладки", "phone": "+75550001122", "tags": ["crm"]})
    assert st == 200, d
    st, d = _call(pool, "GET", "/api/miniapp/crm/contacts")
    assert st == 200, d
    names = {c["first_name"] for c in d["contacts"]}
    assert names == {"Из контактов", "Из вкладки"} and d["total"] == 2
    assert set(d["all_tags"]) == {"hub", "crm"}
    mine = next(c for c in d["contacts"] if c["first_name"] == "Из вкладки")
    assert mine["phone"] == "+75550001122" and mine["source"] == "CRM"
    st, d2 = _call(pool, "GET", "/api/miniapp/crm/contacts?q=" + "вкладки")
    assert [c["first_name"] for c in d2["contacts"]] == ["Из вкладки"] and d2["total"] == 1
    st, d3 = _call(pool, "GET", "/api/miniapp/crm/contacts?q=" + "5550001")
    assert [c["first_name"] for c in d3["contacts"]] == ["Из вкладки"], "поиск по телефону пропал"
    st, _ = _call(pool, "DELETE", "/api/miniapp/crm/contact/" + mine["id"])
    assert st == 200
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1", OWNER)) == 0, (
        "в старой таблице остался удалённый — старые читатели напишут ему")
    st, d = _call(pool, "GET", "/api/miniapp/crm/contacts")
    assert [c["first_name"] for c in d["contacts"]] == ["Из контактов"]
    st, _ = _call(pool, "DELETE", "/api/miniapp/crm/contact/not-a-uuid")
    assert st == 400


def test_nobody_reads_the_old_table_any_more():
    """Шаг 4: старую таблицу читают только перенос (legacy_crm) и удаление во
    вкладке «Клиенты», которое снимает пару строки вместе с контактом. Новый читатель crm_contacts снова
    развёл бы две базы: «вижу во вкладке» и «пишу рассылкой» разошлись бы."""
    import re
    allowed = {"services/contacts_hub/legacy_crm.py"}
    bad = []
    for path in list((ROOT / "services").rglob("*.py")) + list((ROOT / "bot").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        code = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                         if not l.lstrip().startswith("#"))
        if re.search(r"\b(FROM|JOIN|INTO|UPDATE)\s+crm_contacts\b", code):
            bad.append(rel)
    assert not bad, f"снова читают старую таблицу вместо «Контактов»: {bad}"
    src = (ROOT / "services/mini_app_broadcast.py").read_text(encoding="utf-8")
    body = src[src.index("async def crm_contacts("):src.index("async def crm_contact_create(")]
    assert "FROM crm_contacts" not in body, "вкладка «Клиенты» снова читает старую таблицу"


def test_crm_audience_is_clients_inside_contacts(pool):
    """Аудитория «CRM» — люди с меткой из «Клиентов», с настоящим ID и не
    исключённые из рассылок."""
    from services.contacts_hub.legacy_crm import CRM_ROWS
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(pool.execute(
        """INSERT INTO unified_contacts (owner_id, telegram_user_id, first_name, custom_fields, excluded)
           VALUES ($1, 6_000_000_001, 'клиент', '{"legacy_crm_id": 1}', FALSE),
                  ($1, NULL,          'без id', '{"legacy_crm_id": 2}', FALSE),
                  ($1, 6_000_000_003, 'исключён', '{"legacy_crm_id": 3}', TRUE),
                  ($1, 6_000_000_004, 'не клиент', '{}', FALSE)""", OWNER))
    ids = _run(pool.fetch(
        f"SELECT tg_user_id FROM {CRM_ROWS} c WHERE owner_id=$1 AND tg_user_id > 0", OWNER))
    assert [r["tg_user_id"] for r in ids] == [6_000_000_001]


def test_clients_added_after_step5_are_crm_audience(pool):
    """Клиент, заведённый во вкладке после шага 5, — в аудитории «CRM», без
    придуманного ID; повтор с тем же @username не заводит второго."""
    from services.contacts_hub.legacy_crm import CRM_ROWS, add_crm_client
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    a = _run(add_crm_client(pool, OWNER, tg_user_id=6_100_000_001, username="@neo",
                            first_name="Нео", tags=["vip"]))
    b = _run(add_crm_client(pool, OWNER, username="neo", phone="+70001112233"))
    assert a and a == b, "тот же человек заведён дважды"
    c = _run(add_crm_client(pool, OWNER, first_name="Без ID"))
    rows = _run(pool.fetch(f"SELECT * FROM {CRM_ROWS} x WHERE owner_id=$1", OWNER))
    assert {r["first_name"] for r in rows} == {"Нео", "Без ID"}
    assert next(r for r in rows if r["first_name"] == "Без ID")["tg_user_id"] is None
    assert c
