"""Дозор флота: чужие сеансы и охват проверки спам-блока.

Найдено аудитом жизненного цикла аккаунта (12.11.2026):

1. Угон не был виден вообще. Ни один путь не спрашивал account.getAuthorizations:
   продавец купленного аккаунта или угонщик сидел рядом с нашим сеансом, а флот
   узнавал об этом только по умершей сессии — когда вернуть аккаунт уже нечем.
   Теперь фоновая проверка сессий (раз в 6 часов по всему флоту) снимает чужие
   авторизации тем же соединением, пишет их в аккаунт и тревожит владельца
   о НОВОМ сеансе (без повтора на каждом проходе).

2. Проверка через @SpamBot шла по 10 аккаунтов раз в 6 часов на всю платформу
   и выбирала по отметке, которую каждые 3–6 часов освежает пинг живости, —
   то есть почти никогда не видела флот. Теперь своя отметка, сутки на аккаунт
   и размер круга по флоту.

3. «Закрыть чужие сеансы» из сеанса моложе суток Telegram запрещает
   (FRESH_RESET_AUTHORISATION_FORBIDDEN) — владелец видел английский код.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

from services import account_health as ah
from services import account_manager as am
from services import session_watch as sw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Чужие сеансы: снятие списка ─────────────────────────────────────────────

class _Auth:
    def __init__(self, h, current=False, **kw):
        self.hash = h
        self.current = current
        self.device_model = kw.get("device", "iPhone 15")
        self.platform = "iOS"
        self.app_name = "Telegram iOS"
        self.app_version = "11.2"
        self.official_app = True
        self.ip = "203.0.113.5"
        self.country = "Netherlands"
        self.date_created = 1_700_000_000
        self.date_active = 1_700_000_500


class _Client:
    def __init__(self, auths):
        self._auths = auths

    async def __call__(self, request):
        return type("R", (), {"authorizations": self._auths})()


def test_foreign_authorizations_skip_our_own_session():
    out = asyncio.run(am.foreign_authorizations(
        _Client([_Auth(0, current=True), _Auth(11), _Auth(22, device="Pixel 8")])))
    assert [s["hash"] for s in out] == [11, 22]
    assert out[1]["device"] == "Pixel 8" and out[0]["country"] == "Netherlands"


def test_failed_request_is_none_not_an_empty_list():
    """Пустой список = «чужих нет», и он стёр бы отпечатки; сбой — это None."""
    class _Broken:
        async def __call__(self, request):
            raise RuntimeError("timeout")
    assert asyncio.run(am.foreign_authorizations(_Broken())) is None


# ── Чужие сеансы: запись и тревога ──────────────────────────────────────────

class _Pool:
    def __init__(self, fail=False):
        self.executed = []
        self.fail = fail

    async def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append((sql, args))
        return "UPDATE 1"


def _patch_notify(monkeypatch):
    from database import db
    sent = []

    async def _ok(pool, owner, key, cooldown):
        return True

    async def _notify(pool, bot, owner, pref, text, reply_markup=None, *, dedup_key=None):
        sent.append((owner, pref, text, dedup_key))
        return True

    async def _forget(*a, **k):
        return None

    monkeypatch.setattr(db, "notify_dedup_ok", _ok)
    monkeypatch.setattr(db, "notify_if_enabled", _notify)
    monkeypatch.setattr(db, "notify_dedup_forget", _forget)
    return sent


def _foreign(*hashes):
    return [{"hash": h, "device": "iPhone 15", "app": "Telegram iOS 11.2",
             "country": "Netherlands", "ip": "203.0.113.5", "created": 1_700_000_000}
            for h in hashes]


def test_new_foreign_session_alerts_the_owner_once(monkeypatch):
    sent = _patch_notify(monkeypatch)
    pool = _Pool()
    acc = {"id": 5, "owner_id": 7, "username": "acc5", "foreign_session_hashes": None}
    res = asyncio.run(sw.record(pool, acc, _foreign(11), bot=object()))
    assert res == {"foreign": 1, "new": 1, "written": True}
    assert len(sent) == 1 and sent[0][1] == "restriction"
    text = sent[0][2]
    assert "вошли с другого устройства" in text and "@acc5" in text
    assert "iPhone 15" in text and "Netherlands" in text
    assert "Закрыть" not in text or "Очистка и сеансы" in text
    sql, args = pool.executed[0]
    assert "foreign_sessions" in sql and args[0] == 1 and args[1] == [11]

    # Следующий проход с тем же сеансом — тишина.
    acc["foreign_session_hashes"] = [11]
    asyncio.run(sw.record(pool, acc, _foreign(11), bot=object()))
    assert len(sent) == 1, "тревога о давно показанном сеансе повторилась"

    # Появился ещё один — тревога только о нём.
    asyncio.run(sw.record(pool, acc, _foreign(11, 33), bot=object()))
    assert len(sent) == 2 and "Новых чужих сеансов: <b>1</b>" in sent[1][2]


def test_unknown_list_touches_nothing(monkeypatch):
    sent = _patch_notify(monkeypatch)
    pool = _Pool()
    res = asyncio.run(sw.record(pool, {"id": 5, "owner_id": 7}, None, bot=object()))
    assert res == {"skipped": True} and not pool.executed and not sent


def test_no_alert_when_the_record_did_not_land(monkeypatch):
    """Не записали отпечатки — не тревожим: иначе тревога шла бы каждый проход."""
    sent = _patch_notify(monkeypatch)
    res = asyncio.run(sw.record(_Pool(fail=True), {"id": 5, "owner_id": 7},
                                _foreign(11), bot=object()))
    assert res["written"] is False and not sent


def test_alert_escapes_telegram_supplied_text():
    """Устройство и страну пишет тот, кто вошёл, — это ДАННЫЕ, а не разметка."""
    text = sw.build_alert("x", [{"hash": 1, "device": "<b>evil</b>", "app": "",
                                 "created": 0}], 1)
    assert "<b>evil</b>" not in text and "&lt;b&gt;evil" in text


def test_the_six_hour_sweep_collects_foreign_sessions():
    src = _src("services/account_manager.py")
    body = src.split("async def _check_all_sessions(", 1)[1].split("\nasync def ", 1)[0]
    assert "check_sessions=True" in body
    assert "session_watch" in body and ".record(" in body
    assert "foreign_session_hashes" in body, "выборка без отпечатков — тревога на каждом проходе"
    assert 'run_session_health_monitor, pool, bot)' in _src("main.py"), (
        "монитор сессий запущен без бота — тревога о чужом сеансе не уйдёт")


def test_closing_sessions_resets_the_watch():
    ow = _src("services/op_worker.py")
    assert 'elif op == "close_sessions":' in ow and "_sw.forget(" in ow
    inline = _src("services/mini_app_accounts.py")
    assert "_sw.forget(pool, uid, acc_id)" in inline


def test_fresh_session_refusal_is_explained_in_russian():
    from services import profile_setter_engine as pse
    exc = type("FreshResetAuthorisationForbiddenError", (Exception,), {})(
        "The current session is too new (caused by ResetAuthorizationsRequest)")
    msg = pse.close_sessions_error_ru(exc)
    assert "моложе" in msg and "24 часа" in msg
    exc2 = RuntimeError("RPCError 406: FRESH_RESET_AUTHORISATION_FORBIDDEN")
    assert "моложе" in pse.close_sessions_error_ru(exc2)


# ── Охват проверки через @SpamBot ───────────────────────────────────────────

def test_spambot_batch_covers_the_fleet_in_a_day():
    assert ah.spambot_batch_size(0) == ah._SPAMBOT_MIN_BATCH
    assert ah.spambot_batch_size(120) == ah._SPAMBOT_MIN_BATCH
    assert ah.spambot_batch_size(2400) == 100      # 2400 / 24 круга
    assert ah.spambot_batch_size(10**6) == ah._SPAMBOT_MAX_BATCH
    for fleet in (240, 1000, 3000):
        b = ah.spambot_batch_size(fleet)
        assert b * 24 >= fleet, f"флот {fleet} не укладывается в сутки"


def test_spambot_check_has_its_own_mark_and_runs_every_cycle():
    src = _src("services/account_health.py")
    cyc = src.split("async def _run_spambot_check_cycle(", 1)[1].split("\nasync def ", 1)[0]
    assert "last_spambot_check_at" in cyc
    assert "last_real_check_at <" not in cyc, (
        "выборка снова по отметке пинга живости — спам-проверка не видит флот")
    assert "LIMIT 10" not in cyc
    loop = src.split("async def run_health_check_loop(", 1)[1]
    assert "cycle % 6 == 0" not in loop.split("_run_spambot_check_cycle")[0][-400:]


def test_spambot_check_marks_its_own_column(monkeypatch):
    calls = []

    class _P:
        async def execute(self, sql, *a):
            calls.append(sql)

    async def _check(sess, acc, check_spambot=True):
        return {"status": "active"}

    async def _claim(_id):
        return True

    async def _release(_ids):
        return None

    from services import op_worker
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    asyncio.run(ah._spambot_check_one(_P(), {"id": 1, "session_str": "s"}, _check))
    assert any("last_spambot_check_at=now()" in c for c in calls), calls


def test_failed_spambot_check_does_not_hog_the_queue(monkeypatch):
    calls = []

    class _P:
        async def execute(self, sql, *a):
            calls.append((sql, a))

    async def _check(sess, acc, check_spambot=True):
        raise RuntimeError("proxy dead")

    async def _claim(_id):
        return True

    async def _release(_ids):
        return None

    from services import op_worker
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    asyncio.run(ah._spambot_check_one(_P(), {"id": 1, "session_str": "s"}, _check))
    assert any("last_spambot_check_at = now() - make_interval" in c[0] for c in calls)


# ── Живая база: запросы дозора принимает Postgres ───────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995401
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
        pytest.skip("нужен живой Postgres (INFRAGRAM_TEST_DSN)")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = [os.path.join(ROOT, "schema.sql")] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        from database.db import SCHEMA_MIGRATIONS_DDL
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(p.close())


def _acc(pool, **cols):
    async def _s():
        return await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status) "
            "VALUES($1, $2, 'sess-xxxxxxxxxxxx', TRUE, 'active') RETURNING id",
            OWNER, f"+7999{os.urandom(3).hex()}")
    return _run(_s())


def test_live_spambot_cycle_picks_due_accounts_and_marks_them(pool, monkeypatch):
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    fresh = _acc(pool)
    checked_yesterday = _acc(pool)
    checked_now = _acc(pool)
    _run(pool.execute("UPDATE tg_accounts SET last_spambot_check_at=now()-interval '25 hours' "
                      "WHERE id=$1", checked_yesterday))
    _run(pool.execute("UPDATE tg_accounts SET last_spambot_check_at=now(), "
                      "last_real_check_at=now()-interval '30 hours' WHERE id=$1", checked_now))
    seen = []

    async def _check(sess, acc, check_spambot=True):
        seen.append(acc["id"])
        return {"status": "active"}

    async def _claim(_id):
        return True

    async def _release(_ids):
        return None

    async def _nosleep(_s):
        return None

    from services import op_worker
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(am, "check_account_status_full", _check)
    monkeypatch.setattr(ah.asyncio, "sleep", _nosleep)
    # Общая база прогона полна чужих аккаунтов: круг шире всех, чтобы проверка
    # смотрела на выбор по отметке, а не на то, кто первым влез в лимит.
    monkeypatch.setattr(ah, "spambot_batch_size", lambda n: 100000)
    _run(ah._run_spambot_check_cycle(pool))
    mine = {fresh, checked_yesterday, checked_now}
    assert set(seen) & mine == {fresh, checked_yesterday}
    marks = _run(pool.fetch("SELECT id, last_spambot_check_at > now() - interval '1 minute' AS m "
                            "FROM tg_accounts WHERE owner_id=$1", OWNER))
    assert {r["id"] for r in marks if r["m"]} == mine


def test_live_session_watch_round_trip(pool):
    acc_id = _acc(pool)
    _run(sw.record(pool, {"id": acc_id, "owner_id": OWNER}, _foreign(11, 2**62)))
    row = _run(pool.fetchrow("SELECT foreign_sessions, foreign_session_hashes, "
                             "sessions_checked_at FROM tg_accounts WHERE id=$1", acc_id))
    assert row["foreign_sessions"] == 2 and list(row["foreign_session_hashes"]) == [11, 2**62]
    assert row["sessions_checked_at"] is not None
    _run(sw.forget(pool, OWNER, acc_id))
    row = _run(pool.fetchrow("SELECT foreign_sessions, foreign_session_hashes "
                             "FROM tg_accounts WHERE id=$1", acc_id))
    assert row["foreign_sessions"] == 0 and row["foreign_session_hashes"] is None


def test_live_sweep_query_accepts_the_watch_columns(pool):
    from database.db import telethon_accounts_query as _tq
    src = _src("services/account_manager.py")
    body = src.split("async def _check_all_sessions(", 1)[1].split("\nasync def ", 1)[0]
    chunk = body.split("extra_cols=(", 1)[1].split("),\n", 1)[0]
    extra = "".join(re.findall(r'"([^"]*)"', chunk))
    assert "foreign_session_hashes" in extra and "twofa_saved" in extra
    _run(pool.fetch(_tq("a.owner_id = $1", extra_cols=extra), OWNER))


# ── Чужие сеансы видны на экранах ───────────────────────────────────────────

def test_health_verdict_puts_theft_first_and_opens_the_slice():
    from services.mini_app_api import ACCOUNT_FILTERS, fleet_verdict
    v = fleet_verdict({"total": 10, "active": 10, "foreign": 2, "cooling": 3}, [])
    assert v["level"] == "crit"
    first = v["reasons"][0]
    assert first["key"] == "foreign" and first["filter"] == "foreign"
    assert "foreign" in ACCOUNT_FILTERS


def test_list_and_health_count_the_same_slice():
    api = _src("services/mini_app_api.py")
    assert "COUNT(*) FILTER (WHERE {_FOREIGN_SQL}) AS foreign" in api
    acc = _src("services/mini_app_accounts.py")
    assert 'elif flt == "foreign":' in acc and "clauses.append(_FOREIGN_SQL)" in acc
    assert "COALESCE(foreign_sessions, 0) AS foreign_sessions" in acc


def test_account_row_and_health_tile_show_it():
    js = _src("mini_app/screens/accounts_list.js")
    assert "a.foreign_sessions > 0" in js and "${theftNote}" in js
    assert "go('foreign', d.foreign||0, 'Чужие сеансы'" in _src("mini_app/index.html")


# ── Аккаунт выпал — владелец узнаёт с обхода, а не через дни ────────────────

def test_lost_account_alert_names_theft_and_the_saved_password(monkeypatch):
    sent = _patch_notify(monkeypatch)
    acc = {"id": 5, "owner_id": 7, "username": "acc5", "foreign_session_hashes": [11, 22],
           "twofa_saved": True}
    asyncio.run(sw.notify_lost(_Pool(), acc, "session_expired", bot=object()))
    assert len(sent) == 1
    owner, pref, text, key = sent[0]
    assert key == "sess_expired:5", "ключ разошёлся с частым монитором — придёт два сообщения"
    assert "угон" in text and "Пароли 2FA" in text and "@acc5" in text


def test_the_sweep_reports_lost_accounts():
    src = _src("services/account_manager.py")
    body = src.split("async def _check_all_sessions(", 1)[1].split("\nasync def ", 1)[0]
    assert "_sw.notify_lost(" in body
    assert 'f"sess_expired:{acc[\'id\']}"' in _src("services/account_monitor.py")
