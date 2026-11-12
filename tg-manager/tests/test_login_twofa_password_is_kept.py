"""Пароль 2FA, которым вошли в аккаунт, сохраняется у аккаунта.

Найдено аудитом жизненного цикла аккаунта (12.11.2026): колонка `twofa_enc`
(v331) заполнялась только оформлением, которое САМО ставит пароль. Пароль,
введённый владельцем при входе (по номеру и по QR — бот, мини-апп, авторег),
не сохранялся нигде. Сессия такого аккаунта умирает (сброс сессий, смена
прокси, AUTH_KEY_DUPLICATED) — и войти заново можно только с паролем, который
владелец ввёл один раз и не записал: аккаунт потерян для флота, а
восстановление и «Пароли 2FA» его не видят.

Тесты на заглушках проверяют, что пароль доходит до двери записи; тест на
живом Postgres — что он ложится в базу зашифрованным и не затирается пустым.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

from services import account_manager as am

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


class _Me:
    id = 777
    phone = "79990001122"
    first_name = "A"
    username = "u"


class _Client:
    def __init__(self):
        self.session = type("S", (), {"save": lambda self: "SESSION"})()

    async def sign_in(self, *a, **k):
        return None

    async def get_me(self):
        return _Me()


def test_phone_login_with_2fa_hands_the_password_to_the_writer():
    phone = "+79990001122"
    am._pending[phone] = _Client()
    am._pending_device[phone] = {"device_model": "X"}
    try:
        asyncio.run(am.confirm_2fa(phone, "s3cret"))
        _sess, info = asyncio.run(am.get_client_info_and_session(phone))
    finally:
        am._pending.pop(phone, None)
        am._pending_device.pop(phone, None)
    assert info["twofa_password"] == "s3cret"
    assert info["device_model"] == "X"


def test_qr_login_with_2fa_hands_the_password_to_the_writer():
    am._pending_qr[4242] = (_Client(), None, {"device_model": "Y"})
    try:
        _sess, info = asyncio.run(am.confirm_qr_2fa(4242, "qr-pass"))
    finally:
        am._pending_qr.pop(4242, None)
    assert info["twofa_password"] == "qr-pass"


@pytest.mark.parametrize("rel,count", [
    ("services/mini_app_accounts.py", 1),     # _persist_login: телефон и QR
    ("bot/handlers/auto_registrar.py", 1),    # _save_account
    ("bot/handlers/accounts.py", 3),          # QR, QR+2FA, телефон (+2FA)
])
def test_every_login_writer_passes_the_password(rel, count):
    src = _src(rel)
    assert src.count('twofa_password=info.get("twofa_password")') >= count, (
        f"{rel}: запись аккаунта после входа не передаёт пароль 2FA — он потеряется")


def test_shared_door_is_used_by_the_bulk_2fa_setter_too():
    src = _src("services/op_worker.py")
    body = src.split("async def _remember_twofa(", 1)[1].split("\nasync def ", 1)[0]
    assert "db.remember_account_twofa(" in body


# ── Живая база ───────────────────────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995301
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
        # Потолок ожидания блокировки: в наборе схему уже накатили, а чужой
        # тест мог оставить соединение с открытой транзакцией на той же
        # таблице. Без потолка DDL ждёт его вечно, и часть набора висит до
        # таймаута CI (поймано 12.11.2026 на op_telemetry, schema_v113).
        conn = await asyncpg.connect(DSN, server_settings={"lock_timeout": "3000"})
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(os.path.join(ROOT, f), encoding="utf-8").read())
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


def test_password_is_stored_encrypted_and_not_wiped_by_relogin(pool, monkeypatch):
    from database import db
    from services.token_vault import decrypt_token

    async def _nothing(*a, **k):
        return None

    import services.contacts_hub.device_sync as ds
    monkeypatch.setattr(ds, "schedule_for_new_account", _nothing)
    monkeypatch.setattr(db, "check_account_cap", _nothing)
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))

    acc_id, _c, _p = _run(db.upsert_tg_account(
        pool, OWNER, "+79995551234", "sess-1", 556001, twofa_password="pa$$"))
    enc = _run(pool.fetchval("SELECT twofa_enc FROM tg_accounts WHERE id=$1", acc_id))
    assert enc and "pa$$" not in enc, "пароль не сохранён или лежит открытым текстом"
    assert decrypt_token(enc) == "pa$$"

    # Повторный вход без 2FA (например, импорт сессии) пароль не затирает.
    again, _c, _p = _run(db.upsert_tg_account(pool, OWNER, "+79995551234", "sess-2", 556001))
    assert again == acc_id
    enc2 = _run(pool.fetchval("SELECT twofa_enc FROM tg_accounts WHERE id=$1", acc_id))
    assert decrypt_token(enc2) == "pa$$"


# ── Свой случайный пароль каждому аккаунту ──────────────────────────────────

def test_random_password_mode_needs_no_typed_password():
    from services import profile_setter_engine as pse
    p = pse.build_setter_params("2fa", {"random_password": True})
    assert p["random_password"] is True and p["new_password"] == ""
    with pytest.raises(ValueError):
        pse.build_setter_params("2fa", {"new_password": "1"})


def _drive_executor(monkeypatch, apply_ok=True, remember_ok=True):
    from services import op_worker as ow
    from services import profile_setter_engine as pse
    from database import db

    accs = [{"id": 1, "session_str": "s1"}, {"id": 2, "session_str": "s2"}]
    store = {1: "ENC:old1", 2: None}
    applied = []

    class _Pool:
        async def fetchval(self, sql, acc_id, owner):
            return store[acc_id]

        async def execute(self, sql, *a):
            if "twofa_enc" in sql:
                store[a[1]] = a[0]
            return "UPDATE 1"

    async def _sel(*a, **k):
        return list(accs)

    async def _claim(op_id, accounts, owner_id):
        return accounts

    async def _quar(pool, op_id, accounts):
        return accounts, 0

    async def _empty(*a, **k):
        return set()

    async def _none(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    async def _zero(*a, **k):
        return 0.0

    async def _apply(sess, acc, op, params, slot=0):
        applied.append((acc["id"], params.get("new_password")))
        return {"ok": apply_ok, "error": None if apply_ok else "PASSWORD_HASH_INVALID"}

    async def _remember(pool, owner_id, acc_id, pw):
        if not remember_ok:
            return False
        from services.token_vault import encrypt_plaintext_token
        store[acc_id] = encrypt_plaintext_token(pw) if pw else None
        return True

    monkeypatch.setattr(ow.resource_selector, "select_all_active", _sel)
    monkeypatch.setattr(ow, "_claim_available_accounts", _claim)
    monkeypatch.setattr(ow, "_filter_quarantined_accounts", _quar)
    monkeypatch.setattr(ow, "completed_targets", _empty)
    monkeypatch.setattr(ow, "_journal_done", _none)
    monkeypatch.setattr(ow, "_journal_write", _none)
    monkeypatch.setattr(ow, "_safe_execute", _none)
    monkeypatch.setattr(ow, "_is_cancelled", _false)
    monkeypatch.setattr(ow, "_governed_delay", _zero)
    monkeypatch.setattr(ow, "_stored_twofa", lambda *a: _none())
    monkeypatch.setattr(pse, "apply_op", _apply)
    monkeypatch.setattr(db, "remember_account_twofa", _remember)

    async def _sleep(_s):
        return None
    monkeypatch.setattr(ow.asyncio, "sleep", _sleep)
    res = asyncio.run(ow._exec_bulk_set_profile(
        _Pool(), None, 9, 7, {"op": "2fa", "random_password": True, "new_password": "",
                              "account_ids": [1, 2]}))
    return res, store, applied


def test_each_account_gets_its_own_password_and_it_is_kept(monkeypatch):
    from services.token_vault import decrypt_token
    res, store, applied = _drive_executor(monkeypatch)
    assert res["ok"] == 2
    pw1, pw2 = applied[0][1], applied[1][1]
    assert pw1 and pw2 and pw1 != pw2 and len(pw1) >= 12, "пароль общий или слабый"
    assert decrypt_token(store[1]) == pw1 and decrypt_token(store[2]) == pw2


def test_failed_set_restores_the_previous_password_record(monkeypatch):
    res, store, applied = _drive_executor(monkeypatch, apply_ok=False)
    assert res["ok"] == 0
    assert store == {1: "ENC:old1", 2: None}, "запись пароля, который не встал, осталась"


def test_password_that_could_not_be_saved_is_never_set(monkeypatch):
    res, store, applied = _drive_executor(monkeypatch, remember_ok=False)
    assert applied == [], "поставили пароль, который никто не знает"
    assert res["ok"] == 0 and res["failed"] == 2
