"""Модуль аккаунтов: одна дверь на каждую запись — на живом Postgres.

Что здесь закрыто (номера — `docs/project_state/OPEN_DEFECTS.md`).

D4. План прогрева заводили шесть путей своими запросами. Карточка аккаунта не
    проверяла пригодность — план ложился на забаненный аккаунт и стоял на
    «дне 0» вечно; бот обнулял день идущего плана, мини-апп — нет. Теперь все
    пути идут через `account_warmer.create_warmup_plan`.
D9. «Создать планы для всех» в боте брала все включённые аккаунты и обнуляла
    день каждому, кто уже грелся, а спам-блокнутых возвращала под нагрузку.
D6. Аккаунт писался в базу тремя путями. Строка сессии теряла tg_user_id,
    плодила второй аккаунт той же личности и падала «ошибкой БД» на второй
    сессии с пустым номером. Теперь все входы — `db.upsert_tg_account`.
D14. Ручная аппеляция спам-блока шла мимо реабилитации: счётчик аппеляций не
    рос, автоматика писала @SpamBot повторно, а снятый блок записывался
    несуществующим статусом 'ok'.

Каждый тест падает на коде до исправления.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995101
OTHER = 995102
_LOOP = None


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
                pass
        from database.db import SCHEMA_MIGRATIONS_DDL

        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


@pytest.fixture(autouse=True)
def _clean(pool, monkeypatch):
    """Чистый стенд и открытый гейт тарифа: здесь проверяются пути записи,
    а не тариф (его сторожит test_payment_tariff_holes)."""
    from services import operation_bus

    async def _open(*a, **kw):
        return None

    monkeypatch.setattr(operation_bus, "enforce_gates", _open)

    async def _c():
        for owner in (OWNER, OTHER):
            await pool.execute("DELETE FROM account_warmup_plans WHERE owner_id=$1", owner)
            await pool.execute("DELETE FROM account_rehab_state WHERE owner_id=$1", owner)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
    _run(_c())


def _acc(pool, owner=OWNER, status="active", active=True, session="sess", phone=None):
    async def _s():
        return await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
            "VALUES($1,$2,$3,$4,$5) RETURNING id",
            owner, phone or f"+7999{os.urandom(3).hex()}", session, active, status)
    return _run(_s())


def _plan(pool, acc_id):
    return _run(pool.fetchrow(
        "SELECT * FROM account_warmup_plans WHERE account_id=$1", acc_id))


# ── D4: одна дверь создания плана прогрева ──────────────────────────────────

@pytest.mark.parametrize("status,active,session", [
    ("banned", True, "sess"),
    ("spamblock", True, "sess"),
    ("session_expired", True, "sess"),
    ("active", False, "sess"),
    ("active", True, ""),
])
def test_plan_is_not_created_on_an_unfit_account(pool, status, active, session):
    from services import account_warmer as aw

    acc = _acc(pool, status=status, active=active, session=session)
    with pytest.raises(aw.WarmupNotAllowed) as e:
        _run(aw.create_warmup_plan(pool, OWNER, acc, "standard"))
    assert e.value.status in (400, 409)
    assert _plan(pool, acc) is None, "план лёг на аккаунт, который греть нельзя"


def test_account_card_refuses_a_banned_account(pool, monkeypatch):
    """Карточка аккаунта (`/account/{id}/warmup/start`) — тот путь, что заводил
    план на забаненном аккаунте. Проверяем саму ручку."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_accounts, mini_app_api

    acc = _acc(pool, status="banned")
    # Ручки берут _get_uid при регистрации — подменяем до неё.
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: OWNER)
    app = web.Application()
    mini_app_accounts.setup_routes(app, pool)
    handler = next(r.handler for r in app.router.routes()
                   if getattr(r.resource, "canonical", "") ==
                   "/api/miniapp/account/{acc_id}/warmup/start")

    async def _go():
        req = make_mocked_request(
            "POST", f"/api/miniapp/account/{acc}/warmup/start",
            match_info={"acc_id": str(acc)})

        async def _json():
            return {"plan_type": "standard"}

        req.json = _json
        return await handler(req)

    resp = _run(_go())
    assert resp.status == 409, resp.text
    assert _plan(pool, acc) is None


def test_foreign_account_gets_no_plan(pool):
    from services import account_warmer as aw

    acc = _acc(pool, owner=OTHER)
    with pytest.raises(aw.WarmupNotAllowed):
        _run(aw.create_warmup_plan(pool, OWNER, acc, "standard"))
    assert _plan(pool, acc) is None


def test_restart_continues_a_running_plan_and_clears_the_complaint(pool):
    """Перезапуск идущего или приостановленного плана не обнуляет день
    (раньше бот обнулял, мини-апп — нет) и снимает старую жалобу."""
    from services import account_warmer as aw

    acc = _acc(pool)
    pid = _run(aw.create_warmup_plan(pool, OWNER, acc, "standard"))
    _run(pool.execute(
        "UPDATE account_warmup_plans SET current_day=6, status='paused', "
        "pause_reason='user', last_skip_reason='busy' WHERE id=$1", pid))
    pid2 = _run(aw.create_warmup_plan(pool, OWNER, acc, "gentle",
                                      profile_type="reader"))
    p = _plan(pool, acc)
    assert pid2 == pid
    assert p["current_day"] == 6, "перезапуск обнулил неделю прогрева"
    assert p["status"] == "active" and p["plan_type"] == "gentle"
    assert p["pause_reason"] is None and p["last_skip_reason"] is None
    prof = _run(pool.fetchval(
        "SELECT profile_type FROM account_niche_profiles WHERE account_id=$1", acc))
    assert prof == "reader"
    st = _run(pool.fetchval("SELECT acc_status FROM tg_accounts WHERE id=$1", acc))
    assert st == "warming"


def test_finished_plan_starts_over(pool):
    from services import account_warmer as aw

    acc = _acc(pool)
    pid = _run(aw.create_warmup_plan(pool, OWNER, acc, "standard"))
    _run(pool.execute(
        "UPDATE account_warmup_plans SET current_day=14, status='completed', "
        "completed_at=now() WHERE id=$1", pid))
    _run(aw.create_warmup_plan(pool, OWNER, acc, "standard"))
    p = _plan(pool, acc)
    assert p["status"] == "active" and p["current_day"] == 0
    assert p["completed_at"] is None


def test_warmup_op_does_not_revive_a_plan_on_a_banned_account(pool):
    """Исполнитель операции поднимал отменённый план своим UPDATE без проверки
    аккаунта — «Прогрев возобновлён» на забаненном аккаунте."""
    from services import account_warmer as aw
    from services import op_worker

    acc = _acc(pool)
    pid = _run(aw.create_warmup_plan(pool, OWNER, acc, "standard"))
    _run(pool.execute(
        "UPDATE account_warmup_plans SET status='cancelled' WHERE id=$1", pid))
    _run(pool.execute("UPDATE tg_accounts SET acc_status='banned' WHERE id=$1", acc))
    res = _run(op_worker._exec_account_warmup(
        pool, None, 1, OWNER, {"account_id": acc, "plan_type": "standard"}))
    assert res["status"] == "failed", res
    assert _plan(pool, acc)["status"] == "cancelled"


# ── D9: «прогреть все» не трогает идущий прогрев ────────────────────────────

def test_bulk_keeps_running_plans_and_skips_spamblock(pool):
    from services import account_warmer as aw

    running = _acc(pool)
    fresh = _acc(pool)
    blocked = _acc(pool, status="spamblock")
    pid = _run(aw.create_warmup_plan(pool, OWNER, running, "standard"))
    _run(pool.execute(
        "UPDATE account_warmup_plans SET current_day=5 WHERE id=$1", pid))

    res = _run(aw.create_warmup_plans_bulk(pool, OWNER, plan_type="aggressive"))

    assert res["started"] == 1, res
    p = _plan(pool, running)
    assert p["current_day"] == 5 and p["plan_type"] == "standard", (
        "«прогреть все» обнулил или переписал идущий план")
    assert _plan(pool, fresh)["status"] == "active"
    assert _plan(pool, blocked) is None, "спам-блокнутый аккаунт снова под нагрузкой"
    assert _run(aw.count_warmup_candidates(pool, OWNER)) == 0


def test_bulk_over_the_ceiling_says_how_many_are_left(pool):
    """Пакет упёрся в потолок — ответ говорит, сколько осталось без плана,
    а повторное нажатие заводит остаток."""
    from services import account_warmer as aw

    for _ in range(3):
        _acc(pool)
    res = _run(aw.create_warmup_plans_bulk(pool, OWNER, limit=2))
    assert res["started"] == 2 and res["left"] == 1, res
    res = _run(aw.create_warmup_plans_bulk(pool, OWNER, limit=2))
    assert res["started"] == 1 and res["left"] == 0, res


def test_bot_create_all_goes_through_the_shared_bulk():
    import inspect

    from bot.handlers import account_warmup as h

    src = inspect.getsource(h.cb_warmup_create_all_plans)
    assert "create_warmup_plans_bulk(" in src
    assert "create_warmup_plan(" not in src.replace("create_warmup_plans_bulk(", "")


# ── D6: одна запись аккаунта ────────────────────────────────────────────────

def test_empty_phone_becomes_the_telegram_id(pool):
    from database import db

    a1, c1, _ = _run(db.upsert_tg_account(pool, OWNER, "", "s-one", 111001, "А", "a"))
    a2, c2, _ = _run(db.upsert_tg_account(pool, OWNER, "", "s-two", 111002, "Б", "b"))
    assert c1 and c2 and a1 != a2, "вторая сессия с пустым номером перезаписала первую"
    phones = sorted(r["phone"] for r in _run(pool.fetch(
        "SELECT phone FROM tg_accounts WHERE owner_id=$1", OWNER)))
    assert phones == ["id:111001", "id:111002"]


def test_no_phone_and_no_id_is_refused(pool):
    from database import db

    with pytest.raises(db.AccountIdentityMissing):
        _run(db.upsert_tg_account(pool, OWNER, "", "s-x", None, "", ""))
    assert not _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", OWNER))


def test_same_person_with_a_new_session_is_one_account(pool):
    from database import db

    a1, c1, _ = _run(db.upsert_tg_account(pool, OWNER, "+79990001122", "s-a", 222001, "В", "v"))
    _run(pool.execute(
        "UPDATE tg_accounts SET in_operation=TRUE, cooldown_until=now()+INTERVAL '1 day', "
        "api_id=777 WHERE id=$1", a1))
    a2, c2, _ = _run(db.upsert_tg_account(pool, OWNER, "", "s-b", 222001, "", "",
                                          api_id=888))
    assert a2 == a1 and c1 and not c2
    row = _run(pool.fetchrow("SELECT * FROM tg_accounts WHERE id=$1", a1))
    assert row["first_name"] == "В", "пустое имя затёрло известное"
    assert row["in_operation"] is False and row["cooldown_until"] is None
    assert row["api_id"] == 777, "переимпорт сменил приложение живого аккаунта"
    trail = [r["action"] for r in _run(pool.fetch(
        "SELECT action FROM operation_audit WHERE owner_id=$1 AND target=$2 ORDER BY id",
        OWNER, f"account:{a1}"))]
    assert trail[-2:] == ["account_add", "account_reimport"], trail


def test_session_import_keeps_identity_and_does_not_duplicate(pool, monkeypatch):
    """Вкладка «строка сессии»: две сессии с пустым номером — два аккаунта с
    tg_user_id и именем; новая сессия того же человека — обновление, а не дубль."""
    from services import session_importer as SI

    people = {"sessA": 333001, "sessB": 333002, "sessA2": 333001}

    async def _ok(sess, proxy_url=None, timeout_s=None):
        return {"valid": True, "phone": "", "user_id": people[sess],
                "first_name": "Имя", "username": f"u{people[sess]}"}

    monkeypatch.setattr(SI, "validate_session", _ok)
    monkeypatch.setattr(SI, "detect_format", lambda d: "string")
    monkeypatch.setattr(SI, "extract_session_string", lambda d, f: d)

    res = _run(SI.import_sessions(pool, OWNER, "sessA\nsessB"))
    assert res["imported"] == 2 and res["failed"] == 0, res
    res2 = _run(SI.import_sessions(pool, OWNER, "sessA2"))
    assert res2["imported"] == 0 and res2["updated"] == 1, res2

    rows = _run(pool.fetch(
        "SELECT tg_user_id, username FROM tg_accounts WHERE owner_id=$1 ORDER BY tg_user_id",
        OWNER))
    assert [r["tg_user_id"] for r in rows] == [333001, 333002]
    assert rows[0]["username"] == "u333001"


def test_auto_registration_clears_stale_blockers(pool):
    from bot.handlers import auto_registrar

    first = _run(auto_registrar._save_account(
        pool, OWNER, "+79995550000", "s-reg1", {"tg_user_id": 444001}))
    _run(pool.execute(
        "UPDATE tg_accounts SET in_operation=TRUE, trust_score=0 WHERE id=$1", first))
    again = _run(auto_registrar._save_account(
        pool, OWNER, "+79995550000", "s-reg2", {"tg_user_id": 444001}))
    assert again == first
    row = _run(pool.fetchrow("SELECT in_operation, trust_score FROM tg_accounts WHERE id=$1", first))
    assert row["in_operation"] is False and row["trust_score"] >= 1.0


# ── D14: ручная аппеляция — шаг реабилитации ────────────────────────────────

def _rehab(pool, acc):
    return _run(pool.fetchrow("SELECT * FROM account_rehab_state WHERE acc_id=$1", acc))


def _fake_appeal(monkeypatch, status):
    from services import account_manager

    async def _appeal(sess, acc=None):
        return {"ok": status != "error", "status": status, "reply": "ответ", "steps": 2}

    monkeypatch.setattr(account_manager, "appeal_spamblock", _appeal)


def test_manual_appeal_counts_and_defers_the_automatic_one(pool, monkeypatch):
    from services import account_rehab

    acc = _acc(pool, status="spamblock", session="x" * 40)
    _run(pool.execute(
        "INSERT INTO account_rehab_state(acc_id, owner_id, phase, next_action_at) "
        "VALUES($1,$2,'appeal', now())", acc, OWNER))
    _fake_appeal(monkeypatch, "appeal_sent")

    _run(account_rehab.run_manual_appeal(
        pool, OWNER, acc, {"id": acc, "session_str": "x" * 40}))

    r = _rehab(pool, acc)
    assert r["appeal_count"] == 1, "ручная аппеляция не засчитана"
    assert r["phase"] == "warming", "автоматика отправила бы вторую аппеляцию"
    due = _run(pool.fetchval(
        "SELECT next_action_at > now() + INTERVAL '10 minutes' "
        "FROM account_rehab_state WHERE acc_id=$1", acc))
    assert due, "следующий шаг автоматики не отложен"
    audit = _run(pool.fetchval(
        "SELECT COUNT(*) FROM operation_audit WHERE owner_id=$1 AND action='spamblock_appeal'",
        OWNER))
    assert audit >= 1


def test_manual_appeal_that_frees_returns_the_account(pool, monkeypatch):
    from services import account_rehab

    acc = _acc(pool, status="spamblock", session="x" * 40)
    _run(pool.execute(
        "INSERT INTO account_rehab_state(acc_id, owner_id, phase, next_action_at) "
        "VALUES($1,$2,'warming', now())", acc, OWNER))
    _fake_appeal(monkeypatch, "free")

    _run(account_rehab.run_manual_appeal(
        pool, OWNER, acc, {"id": acc, "session_str": "x" * 40}))

    st = _run(pool.fetchval("SELECT acc_status FROM tg_accounts WHERE id=$1", acc))
    assert st == "active", f"снятый блок записан статусом {st!r}"
    assert _rehab(pool, acc)["phase"] == "freed"


def test_manual_appeal_that_confirms_the_block_enrolls_rehab(pool, monkeypatch):
    from services import account_rehab

    acc = _acc(pool, status="active", session="x" * 40)
    _fake_appeal(monkeypatch, "still_blocked")

    _run(account_rehab.run_manual_appeal(
        pool, OWNER, acc, {"id": acc, "session_str": "x" * 40}))

    st = _run(pool.fetchval("SELECT acc_status FROM tg_accounts WHERE id=$1", acc))
    assert st == "spamblock", "подтверждённый блок не отражён в статусе"
    r = _rehab(pool, acc)
    assert r is not None and r["appeal_count"] == 1


def test_manual_appeal_error_does_not_leak_the_exception(pool, monkeypatch):
    from services import account_manager, account_rehab

    acc = _acc(pool, status="spamblock", session="x" * 40)

    async def _boom(sess, acc=None):
        raise RuntimeError("AUTH_KEY_DUPLICATED internal detail")

    monkeypatch.setattr(account_manager, "appeal_spamblock", _boom)
    res = _run(account_rehab.run_manual_appeal(
        pool, OWNER, acc, {"id": acc, "session_str": "x" * 40}))
    assert res["status"] == "error" and "AUTH_KEY" not in str(res)


# ── D13: аккаунт, добавленный в боте, не сидит на IP сервера ────────────────

def _proxies(pool, n):
    async def _s():
        await pool.execute("DELETE FROM user_proxies WHERE owner_id=$1", OWNER)
        ids = []
        for i in range(n):
            ids.append(await pool.fetchval(
                "INSERT INTO user_proxies(owner_id, proxy_url, is_active, is_alive) "
                "VALUES($1,$2,TRUE,TRUE) RETURNING id",
                OWNER, f"socks5://p{i}-{OWNER}:1080"))
        return ids
    return _run(_s())


def test_added_account_is_placed_on_a_proxy_and_keeps_it(pool):
    """add_tg_account (вход в боте: телефон, QR, файл) раскладывает аккаунт по
    прокси владельца, как мини-апп; повторный вход IP живого аккаунта не меняет."""
    from database import db

    p1, p2 = _proxies(pool, 2)
    a = _run(db.add_tg_account(pool, OWNER, "+79991110001", "s-p1", 555001, "", ""))
    b = _run(db.add_tg_account(pool, OWNER, "+79991110002", "s-p2", 555002, "", ""))
    got = _run(pool.fetch(
        "SELECT id, proxy_id FROM tg_accounts WHERE id = ANY($1::bigint[]) ORDER BY id",
        [a, b]))
    assert {r["proxy_id"] for r in got} == {p1, p2}, "аккаунты сели на один IP или без прокси"

    before = got[0]["proxy_id"]
    _run(db.add_tg_account(pool, OWNER, "+79991110001", "s-p1-new", 555001, "", ""))
    after = _run(pool.fetchval("SELECT proxy_id FROM tg_accounts WHERE id=$1", a))
    assert after == before, "повторный вход сменил IP живому аккаунту"

    other = p2 if before == p1 else p1
    _run(db.upsert_tg_account(pool, OWNER, "+79991110001", "s-p1-3", 555001,
                              proxy_id=other, override_proxy=True))
    assert _run(pool.fetchval("SELECT proxy_id FROM tg_accounts WHERE id=$1", a)) == other
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM user_proxies WHERE owner_id=$1", OWNER))
