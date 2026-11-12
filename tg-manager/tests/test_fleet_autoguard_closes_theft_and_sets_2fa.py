"""Автозащита флота: щит сам закрывает чужой вход и ставит пароль 2FA.

Найдено вторым заходом аудита «Защита → Восстановление» (12.11.2026):

1. Дозор чужих сеансов (v340) только ПРЕДУПРЕЖДАЛ. Угонщику, получившему код,
   хватает минут: он ставит свой пароль и закрывает наш сеанс, пока владелец
   читает сообщение. Обход флота раз в 6 часов держит соединение с каждым
   аккаунтом — и мог закрыть новый чужой сеанс сам, тем же соединением.
2. Флот не знал, у каких аккаунтов НЕТ пароля 2FA (account.getPassword
   не спрашивал никто), то есть какие угоняются одним кодом входа. Теперь
   это колонка `twofa_on`, плитка «Без 2FA» и срез списка.
3. Право закрывать чужие сеансы Telegram даёт только сеансу старше суток.
   Новый сеанс, увиденный раньше, не должен становиться «уже показанным» —
   иначе через сутки автозащита его уже не тронула бы.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import time

import pytest

from services import account_manager as am
from services import account_shield as ash
from services import session_watch as sw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = int(time.time()) - 3 * 86400
FRESH = int(time.time()) - 3600


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _foreign(*hashes):
    return [{"hash": h, "device": "iPhone 15", "app": "Telegram iOS", "country": "NL",
             "ip": "203.0.113.5", "created": 1_700_000_000} for h in hashes]


class _Client:
    """Живое соединение аккаунта: закрытие сеанса и установка пароля."""

    def __init__(self, fail_twofa=False, twofa_after_fail=False, log=None):
        self.closed: list[int] = []
        self.twofa: list[str] = []
        self.fail_twofa = fail_twofa
        self.twofa_after_fail = twofa_after_fail
        self.log = log if log is not None else []

    async def __call__(self, req):
        name = type(req).__name__
        if "ResetAuthorization" in name:
            self.closed.append(int(req.hash))
            return True
        if "GetPassword" in name:
            return type("P", (), {"has_password": bool(self.twofa) or self.twofa_after_fail})()
        raise AssertionError(name)

    async def edit_2fa(self, new_password=None, **kw):
        self.log.append("edit_2fa")
        if self.fail_twofa:
            raise RuntimeError("timeout")
        self.twofa.append(new_password)


class _Pool:
    def __init__(self, prev="ENC:prev"):
        self.enc = prev
        self.writes = []

    async def fetchval(self, sql, *a):
        assert "twofa_enc" in sql
        return self.enc

    async def execute(self, sql, *a):
        self.writes.append((sql, a))
        if "SET twofa_enc" in sql:
            self.enc = a[0]
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def _tl_requests(monkeypatch):
    """Заглушка telethon отдаёт безымянные запросы — даём им имена."""
    import telethon.tl.functions.account as acc_fn

    class ResetAuthorizationRequest:
        def __init__(self, hash):
            self.hash = hash

    class GetPasswordRequest:
        pass

    monkeypatch.setattr(acc_fn, "ResetAuthorizationRequest", ResetAuthorizationRequest,
                        raising=False)
    monkeypatch.setattr(acc_fn, "GetPasswordRequest", GetPasswordRequest, raising=False)


@pytest.fixture
def remember(monkeypatch):
    from database import db
    state = {"ok": True, "log": []}

    async def _remember(pool, owner, acc_id, pw):
        state["log"].append("remember")
        if not state["ok"]:
            return False
        pool.enc = "ENC:" + pw
        return True

    monkeypatch.setattr(db, "remember_account_twofa", _remember)
    return state


def _cfg(**kw):
    return ash.ShieldConfig(**kw)


def _acc(known=None):
    return {"id": 5, "owner_id": 7, "username": "acc5", "foreign_session_hashes": known}


# ── Чужие сеансы ─────────────────────────────────────────────────────────────

def test_new_foreign_session_is_closed_and_known_ones_are_left():
    client = _Client()
    prof = {"authorizations": _foreign(11, 22), "own_session_created": OLD, "twofa_on": True}
    out = asyncio.run(sw.guard(_Pool(), client, _acc(known=[11]), prof,
                               _cfg(guard_sessions=True), {}))
    assert client.closed == [22], "закрыт не новый сеанс или закрыт уже показанный"
    assert [s["hash"] for s in out["closed"]] == [22]
    assert [s["hash"] for s in prof["authorizations"]] == [11]


def test_fresh_session_waits_and_the_theft_stays_new_until_it_can_close():
    client = _Client()
    prof = {"authorizations": _foreign(22), "own_session_created": FRESH, "twofa_on": True}
    out = asyncio.run(sw.guard(_Pool(), client, _acc(), prof, _cfg(guard_sessions=True), {}))
    assert client.closed == [] and out["unsettled"] == {22}

    pool = _Pool()
    asyncio.run(sw.record(pool, _acc(), prof["authorizations"], bot=None,
                          unsettled=out["unsettled"]))
    sql, args = pool.writes[0]
    assert args[0] == 1, "счётчик чужих сеансов должен показывать и ждущий"
    assert args[1] == [], "ждущий сеанс запомнен как показанный — через сутки его не закроют"


def test_unknown_session_age_is_treated_as_fresh():
    assert sw.session_old_enough(0) is False
    assert sw.session_old_enough(OLD) is True
    assert sw.session_old_enough(int(time.time()) - 86400) is False, "без запаса на часы"


def test_policy_off_and_exempt_accounts_are_untouched(remember):
    prof = {"authorizations": _foreign(22), "own_session_created": OLD, "twofa_on": False}
    for cfg in (_cfg(), _cfg(guard_sessions=True, guard_twofa=True, exempt_account_ids=[5])):
        client = _Client()
        out = asyncio.run(sw.guard(_Pool(), client, _acc(), dict(prof), cfg, {}))
        assert client.closed == [] and client.twofa == [] and not out["closed"]
    assert remember["log"] == []


# ── Пароль 2FA ───────────────────────────────────────────────────────────────

def test_password_is_saved_before_it_is_set(remember):
    log = remember["log"]
    client = _Client(log=log)
    pool = _Pool(prev=None)
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    out = asyncio.run(sw.guard(pool, client, _acc(), prof, _cfg(guard_twofa=True), {}))
    assert log == ["remember", "edit_2fa"], "пароль ставится раньше, чем сохранён"
    assert out["twofa"] == "set" and prof["twofa_on"] is True
    pw = client.twofa[0]
    assert len(pw) >= 12 and pool.enc == "ENC:" + pw


def test_password_that_could_not_be_saved_is_never_set(remember):
    remember["ok"] = False
    client = _Client()
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    out = asyncio.run(sw.guard(_Pool(), client, _acc(), prof, _cfg(guard_twofa=True), {}))
    assert client.twofa == [] and out["twofa"] not in (None, "set")


def test_refused_password_restores_the_previous_record(remember):
    pool = _Pool(prev="ENC:prev")
    client = _Client(fail_twofa=True)
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    out = asyncio.run(sw.guard(pool, client, _acc(), prof, _cfg(guard_twofa=True), {}))
    assert out["twofa"] not in (None, "set")
    assert pool.enc == "ENC:prev", "осталась запись пароля, который не встал"


def test_lost_answer_after_the_password_landed_keeps_the_record(remember):
    """Ответ потерялся, а пароль встал: запись обязана остаться."""
    pool = _Pool(prev=None)
    client = _Client(fail_twofa=True, twofa_after_fail=True)
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    out = asyncio.run(sw.guard(pool, client, _acc(), prof, _cfg(guard_twofa=True), {}))
    assert out["twofa"] == "set" and pool.enc and pool.enc.startswith("ENC:")


def test_fresh_session_and_budget_hold_the_password_back(remember):
    prof = {"authorizations": [], "own_session_created": FRESH, "twofa_on": False}
    client = _Client()
    asyncio.run(sw.guard(_Pool(), client, _acc(), prof, _cfg(guard_twofa=True), {}))
    assert client.twofa == [], "пароль сразу после входа — признак угона для Telegram"

    budget = {7: sw._TWOFA_PER_OWNER_SWEEP}
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    asyncio.run(sw.guard(_Pool(), client, _acc(), prof, _cfg(guard_twofa=True), budget))
    assert client.twofa == [], "предел паролей за проход не держится"


def test_unknown_password_state_is_not_touched(remember):
    client = _Client()
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": None}
    asyncio.run(sw.guard(_Pool(), client, _acc(), prof, _cfg(guard_twofa=True), {}))
    assert client.twofa == [], "поставили пароль, не узнав, есть ли он"


def test_twofa_probe_reads_has_password():
    class _C:
        def __init__(self, v):
            self.v = v

        async def __call__(self, req):
            if isinstance(self.v, Exception):
                raise self.v
            return type("P", (), {"has_password": self.v})()
    assert asyncio.run(am.twofa_enabled(_C(True))) is True
    assert asyncio.run(am.twofa_enabled(_C(False))) is False
    assert asyncio.run(am.twofa_enabled(_C(RuntimeError("x")))) is None


def test_snapshot_returns_our_own_session_age():
    class _A:
        def __init__(self, h, cur, ts):
            self.hash, self.current, self.date_created, self.date_active = h, cur, ts, ts

    class _C:
        async def __call__(self, req):
            return type("R", (), {"authorizations": [_A(0, True, 1_600_000_000),
                                                     _A(11, False, 1_700_000_000)]})()
    foreign, own = asyncio.run(am.authorizations_snapshot(_C()))
    assert own == 1_600_000_000 and [s["hash"] for s in foreign] == [11]


# ── Отчёт и проводка ─────────────────────────────────────────────────────────

def test_report_is_russian_and_escapes_telegram_text():
    rows = [("@a", {"closed": [{"hash": 1, "device": "<b>x</b>", "created": 0}], "twofa": None}),
            ("@b", {"closed": [], "twofa": "set"}),
            ("@c", {"closed": [], "twofa": "Telegram не принял пароль"})]
    text = sw.build_guard_report(rows)
    assert "Закрыто чужих сеансов: <b>1</b>" in text and "&lt;b&gt;x" in text
    assert "Пароли 2FA" in text and "Пароль не встал: 1" in text
    assert sw.build_guard_report([]) is None


def test_the_sweep_runs_the_guard_on_the_same_connection(monkeypatch):
    """Обход флота: крючок на живом соединении, запись фактов, один отчёт."""
    from services import op_worker
    from database import db

    sent = []

    async def _notify(pool, bot, owner, pref, text, reply_markup=None, *, dedup_key=None):
        sent.append((owner, text))
        return True

    async def _true(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    async def _cfg_for(pool, owner):
        return _cfg(guard_sessions=True)

    async def _check(sess, _acc=None, check_spambot=True, check_sessions=False, on_alive=None):
        prof = {"authorizations": _foreign(22), "own_session_created": OLD, "twofa_on": False}
        await on_alive(_Client(), prof)
        return {"status": "active", "restriction_checked": False, "profile": prof}

    recorded = []

    async def _record(pool, acc, foreign, bot=None, **kw):
        recorded.append((acc["id"], [s["hash"] for s in foreign], kw))
        return {}

    class _P:
        async def fetch(self, *a):
            return [{"id": i, "owner_id": 7, "session_str": "s" * 20, "acc_status": "active",
                     "session_conflict_at": None, "foreign_session_hashes": None,
                     "username": f"a{i}", "twofa_saved": False} for i in (1, 2)]

        async def execute(self, *a):
            return "UPDATE 1"

    monkeypatch.setattr(op_worker, "try_claim_account", _true)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(am, "check_account_status_full", _check)
    monkeypatch.setattr(ash, "get_shield_config", _cfg_for)
    monkeypatch.setattr(sw, "record", _record)
    monkeypatch.setattr(db, "notify_if_enabled", _notify)
    asyncio.run(am._check_all_sessions(_P(), bot=object()))
    assert [r[1] for r in recorded] == [[], []], "закрытый сеанс записан как чужой"
    assert recorded[0][2]["twofa_on"] is False and recorded[0][2]["own_created"] == OLD
    assert len(sent) == 1 and sent[0][0] == 7, "отчёт не один на владельца"
    assert "Закрыто чужих сеансов: <b>2</b>" in sent[0][1]


# ── Экраны и политика ────────────────────────────────────────────────────────

def test_no2fa_slice_is_counted_and_listed_the_same_way():
    from services.mini_app_api import ACCOUNT_FILTERS, fleet_verdict
    assert "no2fa" in ACCOUNT_FILTERS
    api = _src("services/mini_app_api.py")
    assert "COUNT(*) FILTER (WHERE {_NO2FA_SQL}) AS no2fa" in api
    acc = _src("services/mini_app_accounts.py")
    assert 'elif flt == "no2fa":' in acc and "clauses.append(_NO2FA_SQL)" in acc
    v = fleet_verdict({"total": 3, "active": 3, "no2fa": 2}, [])
    r = [x for x in v["reasons"] if x["key"] == "no2fa"][0]
    assert r["filter"] == "no2fa" and "Автозащита" in r["hint"]
    assert "go('no2fa', d.no2fa||0, 'Без 2FA'" in _src("mini_app/index.html")


def test_guard_switches_reach_the_screen_and_the_bot():
    html = _src("mini_app/index.html")
    assert "shieldToggle('guard_sessions')" in html and "shieldToggle('guard_twofa')" in html
    assert '"guard_sessions", "guard_twofa"' in _src("services/mini_app_api.py")
    bot = _src("bot/handlers/account_shield_hub.py")
    assert "toggle_gs" in bot and "toggle_g2" in bot
    assert _cfg().guard_sessions is False and _cfg().guard_twofa is False, (
        "автозащита меняет аккаунты — включает её владелец, а не мы")


# ── Живая база ───────────────────────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995402
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
        conn = await asyncpg.connect(DSN, server_settings={"lock_timeout": "3000"})
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
    _run(p.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER))
    _run(p.close())


def test_live_policy_round_trip(pool):
    _run(pool.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER))
    assert _run(ash.get_shield_config(pool, OWNER)).guard_twofa is False
    cfg = _run(ash.save_shield_config(pool, OWNER, guard_sessions=True, guard_twofa=True))
    assert cfg.guard_sessions and cfg.guard_twofa
    cfg = _run(ash.save_shield_config(pool, OWNER, guard_twofa=False))
    assert cfg.guard_sessions and not cfg.guard_twofa
    assert cfg.as_dict()["guard_sessions"] is True


def test_live_facts_land_and_the_slice_counts_them(pool):
    from services.mini_app_api import _NO2FA_SQL
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))

    async def _new(status="active"):
        return await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status) "
            "VALUES($1, $2, 'sess-xxxxxxxxxxxx', TRUE, $3) RETURNING id",
            OWNER, f"+7999{os.urandom(3).hex()}", status)
    a, b, dead = _run(_new()), _run(_new()), _run(_new("banned"))
    _run(sw.record(pool, {"id": a, "owner_id": OWNER}, _foreign(22), unsettled={22},
                   twofa_on=False, own_created=OLD))
    _run(sw.record(pool, {"id": b, "owner_id": OWNER}, [], twofa_on=True))
    _run(sw.record(pool, {"id": dead, "owner_id": OWNER}, [], twofa_on=False))
    row = _run(pool.fetchrow("SELECT twofa_on, own_session_at, foreign_sessions, "
                             "foreign_session_hashes FROM tg_accounts WHERE id=$1", a))
    assert row["twofa_on"] is False and int(row["own_session_at"].timestamp()) == OLD
    assert row["foreign_sessions"] == 1 and list(row["foreign_session_hashes"]) == []
    # Неизвестное (None) прежнее значение не стирает.
    _run(sw.record(pool, {"id": a, "owner_id": OWNER}, [], twofa_on=None))
    assert _run(pool.fetchval("SELECT twofa_on FROM tg_accounts WHERE id=$1", a)) is False
    n = _run(pool.fetchval(f"SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND {_NO2FA_SQL}",
                           OWNER))
    assert n == 1, "мёртвый аккаунт или аккаунт с паролем попал в «Без 2FA»"
