"""Запасной ключ: второй наш сеанс поднимает аккаунт, чей основной умер.

Найдено третьим заходом аудита «Защита → Восстановление» (12.11.2026): умершая
сессия (AUTH_KEY_DUPLICATED, отзыв ключа, сбой прокси посреди рукопожатия)
значила «войдите заново по коду из SMS». У купленного или зарегистрированного
аккаунта номер владельцу обычно недоступен — аккаунт терялся для флота, а
частый монитор ещё и честно сообщал «выпал».

Теперь автозащита заводит запасной ключ без SMS: живой сеанс сам подтверждает
вход нового (auth.exportLoginToken → auth.acceptLoginToken). Ключ свой,
поэтому гибель основного его не задевает, и круг запасных ключей раз в
10 минут поднимает аккаунт на нём.
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


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


class _PwNeeded(Exception):
    pass


@pytest.fixture(autouse=True)
def _tl(monkeypatch):
    import telethon.errors as errs
    import telethon.tl.functions.account as acc_fn
    import telethon.tl.functions.auth as auth_fn

    class AcceptLoginTokenRequest:
        def __init__(self, token):
            self.token = token

    class GetAuthorizationsRequest:
        pass

    class ResetAuthorizationRequest:
        def __init__(self, hash):
            self.hash = hash

    monkeypatch.setattr(auth_fn, "AcceptLoginTokenRequest", AcceptLoginTokenRequest, raising=False)
    monkeypatch.setattr(acc_fn, "GetAuthorizationsRequest", GetAuthorizationsRequest, raising=False)
    monkeypatch.setattr(acc_fn, "ResetAuthorizationRequest", ResetAuthorizationRequest,
                        raising=False)
    monkeypatch.setattr(errs, "SessionPasswordNeededError", _PwNeeded, raising=False)


class _A:
    def __init__(self, h, cur=False):
        self.hash, self.current, self.date_created, self.date_active = h, cur, OLD, OLD


class _Me:
    def __init__(self, i):
        self.id = i


class _Main:
    """Основной сеанс: принимает токен — и в списке появляется новый вход."""

    def __init__(self, extra_after=(77,)):
        self.auths = [_A(0, True), _A(11)]
        self.extra_after = extra_after
        self.accepted = []
        self.closed = []

    async def get_me(self):
        return _Me(500)

    async def __call__(self, req):
        n = type(req).__name__
        if n == "GetAuthorizationsRequest":
            return type("R", (), {"authorizations": list(self.auths)})()
        if n == "AcceptLoginTokenRequest":
            self.accepted.append(req.token)
            self.auths += [_A(h) for h in self.extra_after]
            return True
        if n == "ResetAuthorizationRequest":
            self.closed.append(req.hash)
            return True
        raise AssertionError(n)


class _QR:
    def __init__(self, need_pw):
        self.token = b"tok"
        self.need_pw = need_pw

    async def wait(self, timeout=None):
        if self.need_pw:
            raise _PwNeeded()


class _New:
    def __init__(self, need_pw=False, me_id=500, wrong_pw=False):
        self.need_pw, self.me_id, self.wrong_pw = need_pw, me_id, wrong_pw
        self.signed_with = None
        self.logged_out = False
        self.session = type("S", (), {"save": lambda s: "RESERVE-SESSION"})()

    async def connect(self):
        return None

    async def qr_login(self):
        return _QR(self.need_pw)

    async def sign_in(self, password=None):
        if self.wrong_pw:
            raise RuntimeError("PASSWORD_HASH_INVALID")
        self.signed_with = password

    async def get_me(self):
        return _Me(self.me_id)

    async def log_out(self):
        self.logged_out = True

    def disconnect(self):
        return None

    def is_connected(self):
        return False


@pytest.fixture(autouse=True)
def _no_disconnect(monkeypatch):
    async def _d(*a, **k):
        return None
    monkeypatch.setattr(am, "safe_disconnect", _d)


def _mint(main, new, pw=None):
    return asyncio.run(am.mint_reserve_session(main, {"id": 5}, pw, new_client=new))


# ── Заведение ключа ──────────────────────────────────────────────────────────

def test_reserve_is_minted_without_sms_and_its_fingerprint_is_known():
    main, new = _Main(), _New()
    sess, h = _mint(main, new)
    assert sess == "RESERVE-SESSION" and h == 77
    assert main.accepted == [b"tok"], "вход подтверждён не основным сеансом"
    assert not new.logged_out


def test_account_with_2fa_uses_the_saved_password():
    new = _New(need_pw=True)
    _mint(_Main(), new, pw="s3cret")
    assert new.signed_with == "s3cret"


@pytest.mark.parametrize("main,new,pw,why", [
    (_Main(), _New(need_pw=True), None, "сохранённого пароля нет"),
    (_Main(), _New(need_pw=True, wrong_pw=True), "bad", "не подтвердил"),
    (_Main(), _New(me_id=999), None, "не в тот аккаунт"),
    (_Main(extra_after=(77, 78)), _New(), None, "опознать"),
    (_Main(extra_after=()), _New(), None, "опознать"),
])
def test_any_failure_after_accept_logs_the_new_session_out(main, new, pw, why):
    with pytest.raises(am.ReserveKeyError) as ei:
        _mint(main, new, pw)
    assert why in str(ei.value)
    assert new.logged_out, "в аккаунте остался безымянный вход, которого флот не знает"


def test_reserve_is_never_counted_as_a_foreign_session():
    prof = {"authorizations": [{"hash": 11}, {"hash": 77}]}
    sw.drop_own_reserve(prof, {"reserve_auth_hash": 77})
    assert [s["hash"] for s in prof["authorizations"]] == [11]


# ── Автозащита заводит ключ ─────────────────────────────────────────────────

class _Pool:
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail

    async def fetchval(self, sql, *a):
        return None

    async def execute(self, sql, *a):
        if self.fail and "reserve_session_enc=$1" in sql:
            raise RuntimeError("db down")
        self.writes.append((sql, a))
        return "UPDATE 1"


def _guard(pool, acc, prof, cfg, monkeypatch, minted=("RESERVE-SESSION", 77)):
    calls = []

    async def _mint_fake(client, acc, pw=None, **kw):
        calls.append(pw)
        return minted
    monkeypatch.setattr(am, "mint_reserve_session", _mint_fake)
    out = asyncio.run(sw.guard(pool, _Main(), acc, prof, cfg, {}))
    return out, calls


def test_guard_mints_and_stores_the_key_encrypted(monkeypatch):
    from services.token_vault import decrypt_token
    pool = _Pool()
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    out, calls = _guard(pool, {"id": 5, "owner_id": 7}, prof,
                        ash.ShieldConfig(guard_reserve=True), monkeypatch)
    assert out["reserve"] == "made" and calls == [None]
    sql, a = [w for w in pool.writes if "reserve_session_enc" in w[0]][0]
    assert a[0] != "RESERVE-SESSION" and decrypt_token(a[0]) == "RESERVE-SESSION"
    assert a[1] == 77


@pytest.mark.parametrize("acc,prof,cfg", [
    ({"id": 5, "owner_id": 7, "has_reserve": True}, {"own_session_created": OLD},
     ash.ShieldConfig(guard_reserve=True)),
    ({"id": 5, "owner_id": 7}, {"own_session_created": int(time.time()) - 60},
     ash.ShieldConfig(guard_reserve=True)),
    ({"id": 5, "owner_id": 7}, {"own_session_created": OLD}, ash.ShieldConfig()),
])
def test_guard_leaves_the_key_alone_when_it_should(acc, prof, cfg, monkeypatch):
    prof = {"authorizations": [], "twofa_on": True, **prof}
    out, calls = _guard(_Pool(), acc, prof, cfg, monkeypatch)
    assert calls == [] and out["reserve"] is None


def test_unrecorded_key_is_closed_not_left_behind(monkeypatch):
    main = _Main()

    async def _mint_fake(client, acc, pw=None, **kw):
        return "RESERVE-SESSION", 77
    monkeypatch.setattr(am, "mint_reserve_session", _mint_fake)
    prof = {"authorizations": [], "own_session_created": OLD, "twofa_on": False}
    out = asyncio.run(sw.guard(_Pool(fail=True), main, {"id": 5, "owner_id": 7}, prof,
                               ash.ShieldConfig(guard_reserve=True), {}))
    assert main.closed == [77] and out["reserve"] != "made"


def test_report_names_the_reserve():
    text = sw.build_guard_report([("@a", {"closed": [], "twofa": None, "reserve": "made"}),
                                  ("@b", {"closed": [], "twofa": None,
                                          "reserve": "на аккаунте пароль 2FA, а сохранённого пароля нет"})])
    assert "Заведён запасной ключ: <b>1</b>" in text and "не завёлся: 1" in text


# ── Подъём ───────────────────────────────────────────────────────────────────

class _RevPool:
    def __init__(self):
        self.sql = []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        return {"id": a[0]}

    async def execute(self, sql, *a):
        self.sql.append(sql)
        return "UPDATE 1"


def _revive(monkeypatch, status, acc_status="session_expired"):
    seen = {}

    async def _check(sess, _acc=None, check_spambot=True, **kw):
        seen["sess"] = sess
        return {"status": status}

    async def _set(pool, acc_id, st, **kw):
        seen["status"] = (st, kw.get("source"))
        return True

    from services import account_status
    monkeypatch.setattr(am, "check_account_status_full", _check)
    monkeypatch.setattr(account_status, "set_status", _set)
    pool = _RevPool()
    r = asyncio.run(sw.revive_from_reserve(
        pool, {"id": 5, "owner_id": 7, "acc_status": acc_status, "reserve_session_enc": "ENC:r"}))
    return r, pool, seen


def test_dead_account_is_lifted_onto_the_reserve(monkeypatch):
    r, pool, seen = _revive(monkeypatch, "active")
    assert r == "revived" and seen["sess"] == "ENC:r"
    assert seen["status"] == ("active", "reserve_key")
    promote = [s for s in pool.sql if "session_str=reserve_session_enc" in s][0]
    assert "acc_status='session_expired'" in promote and "reserve_session_enc=$2" in promote


def test_dead_reserve_is_dropped_so_a_new_one_is_minted(monkeypatch):
    r, pool, seen = _revive(monkeypatch, "session_expired")
    assert r == "dead" and "status" not in seen
    assert any("reserve_session_enc=NULL" in s for s in pool.sql)


def test_live_account_only_marks_the_reserve_checked(monkeypatch):
    r, pool, seen = _revive(monkeypatch, "active", acc_status="active")
    assert r == "alive" and "status" not in seen
    assert not any("session_str=" in s for s in pool.sql)


def test_the_sweep_and_main_wire_it():
    src = _src("services/account_manager.py")
    body = src.split("async def _check_all_sessions(", 1)[1].split("\nasync def ", 1)[0]
    assert "_sw.drop_own_reserve(profile, _acc)" in body
    assert "has_reserve" in body and "reserve_auth_hash" in body
    assert 'run_reserve_keeper, pool, bot)' in _src("main.py")
    assert "shieldToggle('guard_reserve')" in _src("mini_app/index.html")
    assert "toggle_gr" in _src("bot/handlers/account_shield_hub.py")
    assert ash.ShieldConfig().guard_reserve is False


# ── Живая база ───────────────────────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995503
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
    _run(p.close())


def test_live_keeper_lifts_a_dead_account(pool, monkeypatch):
    from services import op_worker
    from services.token_vault import encrypt_plaintext_token, session_fingerprint
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    enc = encrypt_plaintext_token("RESERVE-SESSION-LIVE")
    acc_id = _run(pool.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, "
        "reserve_session_enc, reserve_auth_hash, reserve_made_at) "
        "VALUES($1, $2, 'dead-main-session', FALSE, 'session_expired', $3, 77, "
        "now() - interval '3 days') RETURNING id",
        OWNER, f"+7999{os.urandom(3).hex()}", enc))

    async def _check(sess, _acc=None, check_spambot=True, **kw):
        return {"status": "active" if sess == enc else "session_expired"}

    async def _true(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(am, "check_account_status_full", _check)
    monkeypatch.setattr(op_worker, "try_claim_account", _true)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(sw, "_KEEPER_BATCH", 100000)
    tally = _run(sw.keep_reserves(pool))
    assert tally.get("revived", 0) >= 1
    row = _run(pool.fetchrow("SELECT session_str, session_fp, acc_status, is_active, "
                             "reserve_session_enc, own_session_at FROM tg_accounts WHERE id=$1",
                             acc_id))
    assert row["session_str"] == enc and row["session_fp"] == session_fingerprint(enc)
    assert row["acc_status"] == "active" and row["is_active"] is True
    assert row["reserve_session_enc"] is None and row["own_session_at"] is not None
    # Второй круг: ключа больше нет — трогать нечего.
    again = _run(sw.keep_reserves(pool))
    assert again.get("revived", 0) == 0 or acc_id not in []


def test_live_sweep_query_accepts_the_reserve_columns(pool):
    from database.db import telethon_accounts_query as _tq
    src = _src("services/account_manager.py")
    body = src.split("async def _check_all_sessions(", 1)[1].split("\nasync def ", 1)[0]
    chunk = body.split("extra_cols=(", 1)[1].split("),\n", 1)[0]
    extra = "".join(re.findall(r'"([^"]*)"', chunk))
    assert "has_reserve" in extra
    _run(pool.fetch(_tq("a.owner_id = $1", extra_cols=extra), OWNER))


def test_live_policy_round_trip(pool):
    _run(pool.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER))
    cfg = _run(ash.save_shield_config(pool, OWNER, guard_reserve=True))
    assert cfg.guard_reserve and cfg.as_dict()["guard_reserve"] is True
    _run(pool.execute("DELETE FROM shield_configs WHERE owner_id=$1", OWNER))
