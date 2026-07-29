"""Сквозная матрица инвайта по НАСТОЯЩЕМУ Postgres. Заглушен только Telethon.

ЗАЧЕМ ОТДЕЛЬНО ОТ ОСТАЛЬНЫХ ТЕСТОВ. Заглушка пула не проверяет типы параметров:
фейковый `fetchrow(q, *a)` принимает что угодно, поэтому ошибки СВЯЗЫВАНИЯ
(строка вместо datetime, dict вместо jsonb) на юнит-тестах невидимы в принципе.
Именно так пережил релиз сломанный `operation_bus.submit(scheduled_for=...)`,
который убивал ВСЕ отложенные операции продукта: каст `$5::timestamptz` в запросе
был, но asyncpg выводит тип параметра из запроса и падал на строке ещё до
Postgres. Свод, класс 23.

КАК ЗАПУСТИТЬ (2 минуты, Postgres 16):

    D=/var/tmp/pgtest; mkdir -p $D/pg $D/sock; chown -R postgres $D
    su postgres -s /bin/bash -c "initdb -D $D/pg -U postgres -A trust"
    su postgres -s /bin/bash -c "pg_ctl -D $D/pg \\
        -o \\"-p 55432 -k $D/sock -c listen_addresses=''\\" -l $D/pg/log start"
    psql -h $D/sock -p 55432 -U postgres -c 'CREATE DATABASE infra'
    export INFRAGRAM_TEST_DSN="postgresql://postgres@/infra?host=$D/sock&port=55432"
    pytest tests/test_invite_e2e_postgres.py -v

Без переменной окружения файл пропускается — CI и обычный прогон не ломаются.
Схема накатывается прямо здесь (все `schema*.sql` по возрастанию версии).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER = 990001


# ОДИН цикл на весь модуль. Соединения asyncpg привязаны к тому циклу, в котором
# создан пул: `new_event_loop()` на каждый вызов даёт «Event loop is closed» на
# втором же обращении к БД. С заглушками этой проблемы нет вовсе — ещё одна вещь,
# которую видно только на живом драйвере.
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── стенд ────────────────────────────────────────────────────────────────────

class _Stand:
    """Инвайт целиком, кроме Telethon: настоящая БД, настоящий исполнитель."""

    def __init__(self, pool):
        self.pool = pool
        self.sent: list = []
        self.per_acc: dict = {}
        self.mode = "ok"
        self.flooder = None

    async def _engine(self, session, acc, group, refs):
        self.sent.extend(refs)
        self.per_acc.setdefault(int(acc["id"]), []).extend(refs)
        if self.mode == "flood" and int(acc["id"]) == self.flooder:
            return {"ok": 0, "failed": 1, "peer_flood": True, "errors": ["peer flood"]}
        if self.mode == "group_closed":
            return {"ok": 0, "failed": 1, "errors": ["group error: ChannelPrivateError"]}
        if self.mode == "boom":
            raise RuntimeError("движок упал")
        return {"ok": len(refs), "failed": 0, "errors": []}

    async def seed(self, *, accounts=2, parsed=0, crm=0):
        p = self.pool
        for t in ("operation_queue", "parsed_audiences", "crm_contacts", "invite_target_log"):
            try:
                await p.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
            except Exception:
                pass
        await p.execute("DELETE FROM account_daily_stats WHERE account_id IN "
                        "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        ids = []
        for i in range(accounts):
            ids.append(await p.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "first_name) VALUES($1,$2,$3,TRUE,'active',$4) RETURNING id",
                OWNER, f"+7990{i:07d}", f"sess{i}", f"Акк{i}"))
        for i in range(parsed):
            await p.execute(
                "INSERT INTO parsed_audiences(owner_id,source_type,source_username,"
                "tg_user_id,username) VALUES($1,'channel',$2,$3,$4)",
                OWNER, "ch_a" if i % 2 else "ch_b", 810000 + i, f"p{i}")
        for i in range(crm):
            await p.execute("INSERT INTO crm_contacts(owner_id,tg_user_id,username) "
                            "VALUES($1,$2,$3)", OWNER, 710000 + i, f"c{i}")
        self.sent.clear()
        self.per_acc.clear()
        self.flooder = ids[0] if ids else None
        return ids

    async def run(self, params, total=1):
        """Поставить операцию тем же SQL, что хендлер, и провести через воркер."""
        from services import op_worker as w
        op_id = await self.pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,'mass_invite','pending',$2,$3,'e2e') RETURNING id",
            OWNER, json.dumps(params), total)
        rows = await self.pool.fetch(
            "UPDATE operation_queue SET status='running', started_at=now() WHERE id=$1 "
            "RETURNING id, owner_id, op_type, params", op_id)
        await w._run_op_task(self.pool, None, dict(rows[0]))
        return await self.pool.fetchrow(
            "SELECT status, done_items, result->>'summary' AS summary, error_msg "
            "FROM operation_queue WHERE id=$1", op_id)

    async def carried_over(self) -> set:
        """Цели, уехавшие в продолжения (их нельзя считать потерянными)."""
        out: set = set()
        for row in await self.pool.fetch(
                "SELECT params->>'user_refs' AS r FROM operation_queue "
                "WHERE owner_id=$1 AND params->>'invite_chain' IS NOT NULL", OWNER):
            out |= set(json.loads(row["r"] or "[]"))
        return out


@pytest.fixture(scope="module")
def stand():
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
                pass  # схемы идемпотентны; частичный сбой не должен рушить прогон
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        pool = _run(_boot())
    except Exception as exc:
        # DSN задан, но сервер недоступен (не поднят / упал / не тот порт). Это не
        # провал инвайта — это отсутствие стенда: честный skip вместо 17 трейсбеков,
        # которые прячут реальную причину за деталями asyncpg.
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    s = _Stand(pool)

    import services.mass_inviter_engine as inv
    from services import invite_behavior, op_worker as w
    inv.invite_batch = s._engine
    inv.invite_by_phones = s._engine

    async def _no_behaviour(*a, **k):
        return None
    invite_behavior.humanize = _no_behaviour
    _sleep = asyncio.sleep

    async def _fast(x):
        return await _sleep(0)
    w.asyncio.sleep = _fast

    yield s
    _run(pool.close())
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


# ── единичный инвайт ─────────────────────────────────────────────────────────

def test_single_username(stand):
    _run(stand.seed())
    r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": ["@one"]}))
    assert r["status"] == "done" and len(stand.sent) == 1


def test_single_phone(stand):
    _run(stand.seed())
    r = _run(stand.run({"group": "@g", "source": "import_list", "phones": ["+79991112233"]}))
    assert r["status"] == "done" and len(stand.sent) == 1


def test_single_numeric_id(stand):
    _run(stand.seed())
    r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": [123456789]}))
    assert r["status"] == "done" and len(stand.sent) == 1


# ── массовый инвайт ──────────────────────────────────────────────────────────

def test_mass_twenty_targets(stand):
    _run(stand.seed())
    refs = [f"@m{i}" for i in range(20)]
    r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs}, total=20))
    assert r["status"] == "done"
    assert set(stand.sent) == set(refs)
    assert r["done_items"] == 20, "прогресс обязан сойтись с реально обработанными"


def test_mass_mixed_refs_and_phones(stand):
    _run(stand.seed())
    refs = [f"@x{i}" for i in range(10)]
    phones = [f"+7999000{i:04d}" for i in range(10)]
    r = _run(stand.run({"group": "@g", "source": "import_list",
                        "user_refs": refs, "phones": phones}, total=20))
    assert r["status"] == "done" and len(stand.sent) == 20


# ── источники аудитории ──────────────────────────────────────────────────────

def test_source_parsed(stand):
    _run(stand.seed(parsed=25))
    r = _run(stand.run({"group": "@g", "source": "parsed"}, total=25))
    assert r["status"] == "done" and len(stand.sent) == 25


def test_source_crm(stand):
    _run(stand.seed(crm=12))
    r = _run(stand.run({"group": "@g", "source": "crm"}, total=12))
    assert r["status"] == "done" and len(stand.sent) == 12


def test_empty_source_fails_honestly(stand):
    _run(stand.seed())
    r = _run(stand.run({"group": "@g", "source": "bot_users"}))
    assert r["status"] == "failed" and "пуст" in (r["summary"] or "")


# ── граничные случаи ─────────────────────────────────────────────────────────

def test_missing_group_is_refused(stand):
    _run(stand.seed())
    r = _run(stand.run({"source": "import_list", "user_refs": ["@a"]}))
    assert r["status"] == "failed" and "группа" in (r["summary"] or "").lower()


def test_no_accounts_is_refused(stand):
    _run(stand.seed(accounts=0))
    r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": ["@a"]}))
    assert r["status"] == "failed" and "аккаунт" in (r["summary"] or "").lower()


def test_flood_loses_nothing(stand):
    """Инвариант — НЕ «всё за один прогон» (мешает суточный лимит), а «ничего не
    потеряно»: отданное движку + перенесённое в продолжение = вся аудитория."""
    _run(stand.seed())
    stand.mode = "flood"
    try:
        refs = [f"@f{i}" for i in range(20)]
        r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs},
                           total=20))
        covered = set(stand.sent) | _run(stand.carried_over())
        assert r["status"] == "done"
        assert covered == set(refs), f"потеряно: {sorted(set(refs) - covered)}"
    finally:
        stand.mode = "ok"


def test_closed_group_stops_the_fleet(stand):
    _run(stand.seed())
    stand.mode = "group_closed"
    try:
        refs = [f"@c{i}" for i in range(20)]
        r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs},
                           total=20))
        assert "недоступна" in (r["summary"] or "")
        assert len(stand.sent) <= 5, "флот не должен разбиваться об закрытую группу кругами"
    finally:
        stand.mode = "ok"


def test_engine_exception_does_not_kill_the_operation(stand):
    _run(stand.seed())
    stand.mode = "boom"
    try:
        refs = [f"@b{i}" for i in range(10)]
        r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs},
                           total=10))
        assert r["status"] in ("done", "failed"), "операция обязана завершиться, а не зависнуть"
    finally:
        stand.mode = "ok"


# ── лимиты ───────────────────────────────────────────────────────────────────

def test_per_account_limit_is_respected(stand):
    _run(stand.seed())
    refs = [f"@l{i}" for i in range(20)]
    _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs,
                    "per_account_limit": 3}, total=20))
    assert len(stand.sent) == 6, f"2 аккаунта × 3 = 6, отдано {len(stand.sent)}"


def test_max_invites_is_respected(stand):
    _run(stand.seed())
    refs = [f"@k{i}" for i in range(20)]
    _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs,
                    "max_invites": 7}, total=20))
    assert len(stand.sent) <= 10, f"лимит прогона 7, отдано {len(stand.sent)}"


def test_cold_start_limit_spills_into_a_continuation(stand):
    """Холодный суточный лимит не должен ронять аудиторию — остаток уезжает
    в следующую операцию, а не исчезает."""
    _run(stand.seed(accounts=1))
    refs = [f"@s{i}" for i in range(50)]
    r = _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs},
                       total=50))
    assert r["status"] == "done"
    covered = set(stand.sent) | _run(stand.carried_over())
    assert covered == set(refs), f"потеряно: {sorted(set(refs) - covered)}"
    assert "продолжим завтра" in (r["summary"] or ""), "остаток обязан быть виден"


# ── повторный запуск ─────────────────────────────────────────────────────────

def test_rerun_does_not_touch_the_same_targets(stand):
    _run(stand.seed())
    refs = [f"@d{i}" for i in range(10)]
    _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs}, total=10))
    first = len(stand.sent)
    stand.sent.clear()
    _run(stand.run({"group": "@g", "source": "import_list", "user_refs": refs}, total=10))
    assert first == 10 and len(stand.sent) == 0, (
        "повторный запуск обязан пропускать уже приглашённых"
    )
