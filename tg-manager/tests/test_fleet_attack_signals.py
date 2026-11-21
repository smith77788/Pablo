"""Сигналы атаки на аккаунт флота (services/account_alarm, schema_v379).

Просьба владельца 21.11.2026: защитить флот от взлома, фишинга и сторонних
сессий. Дозор сеансов видел угон, когда вход уже случился; до этого флот не
замечал ничего из того, что видно тем же соединением:

* чужой код входа, после которого никто не вошёл (попытка угона);
* код для my.telegram.org и запрос удаления аккаунта;
* запрос сброса пароля 2FA — его можно отменить из нашего сеанса, и по
  решению владельца продукта отмена включена у всех по умолчанию;
* пароль 2FA, сменённый или снятый не через Infragram;
* новая почта восстановления.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from services import account_alarm as al
from services import account_shield as ash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1_800_000_000


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── Разбор служебного чата ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Login code: 52814. Do not give this code to anyone, even if they say they are from Telegram!",
    "Код подтверждения: 52814. Никому не давайте код, даже если его требуют от имени Telegram!",
    "Код для входа в Telegram: 52814",
])
def test_login_code_is_recognised_in_both_languages(text):
    assert al.classify_service_message(text) == al.KIND_LOGIN_ATTEMPT


def test_web_login_and_deletion_are_recognised():
    assert al.classify_service_message(
        "Web login code. We received a request to log in on my.telegram.org. "
        "This is your login code: AbC9xY") == al.KIND_WEB_LOGIN
    assert al.classify_service_message(
        "We received a request to delete your Telegram account.") == al.KIND_DELETE_REQUEST


def test_new_login_notice_is_left_to_the_session_watch():
    # О новом входе уже тревожит дозор сеансов — вторая тревога была бы шумом.
    assert al.classify_service_message(
        "New login. Dear Ivan, we detected a login into your account from a new "
        "device on 21/11/2026 at 17:00:00 UTC. Device: iPhone") is None
    assert al.classify_service_message("") is None


def _msg(mid, ts, text="Login code: 52814. Do not give this code to anyone"):
    return {"id": mid, "date": ts, "text": text}


def test_code_without_a_login_is_an_attempt():
    ev = al.pick_service_events([_msg(10, NOW - 60)], seen_id=5, login_times=[], now=NOW)
    assert [e["kind"] for e in ev] == [al.KIND_LOGIN_ATTEMPT]


def test_code_followed_by_a_login_is_not_an_attempt():
    # Вошёл наш, доверенный или чужой сеанс: свой не тревожит, чужой
    # тревожит дозор сеансов.
    ev = al.pick_service_events([_msg(10, NOW - 600)], seen_id=5,
                                login_times=[NOW - 540], now=NOW)
    assert ev == []


def test_code_the_owner_read_is_not_an_attempt():
    ev = al.pick_service_events([_msg(10, NOW - 600)], seen_id=5, login_times=[],
                                code_read_at=NOW - 500, now=NOW)
    assert ev == []


def test_already_seen_and_old_history_do_not_alarm():
    assert al.pick_service_events([_msg(10, NOW - 60)], seen_id=10, login_times=[], now=NOW) == []
    # Первый проход: история старше двух часов — не тревога.
    old = al.pick_service_events([_msg(3, NOW - 5 * 3600)], seen_id=None, login_times=[], now=NOW)
    fresh = al.pick_service_events([_msg(4, NOW - 600)], seen_id=None, login_times=[], now=NOW)
    assert old == [] and len(fresh) == 1


# ── Пароль 2FA ───────────────────────────────────────────────────────────────

def test_pending_reset_is_an_event():
    ev = al.password_events({}, {"has_password": True, "pending_reset": NOW + 86400})
    assert ev == [{"kind": al.KIND_RESET_REQUESTED, "until": NOW + 86400}]


def test_removed_password_alarms_only_if_we_knew_it():
    pw = {"has_password": False, "pending_reset": 0}
    assert [e["kind"] for e in al.password_events(
        {"twofa_on": True, "twofa_saved": True}, pw)] == [al.KIND_PASSWORD_REMOVED]
    # Наш собственный сброс стирает запись пароля — это не тревога.
    assert al.password_events({"twofa_on": True, "twofa_saved": False}, pw) == []


def test_changed_password_and_new_recovery_email():
    pw = {"has_password": True, "pending_reset": 0}
    assert [e["kind"] for e in al.password_events({}, pw, verified=False)] == [
        al.KIND_PASSWORD_CHANGED]
    ev = al.password_events({}, pw, verified=True, settings_email="thief@mail.example")
    assert ev[0]["kind"] == al.KIND_RECOVERY_EMAIL and "thief" not in ev[0]["email"]
    same = {"recovery_email_fp": al.email_fp("thief@mail.example")}
    assert al.password_events(same, pw, verified=True, settings_email="thief@mail.example") == []


# ── На живом соединении ──────────────────────────────────────────────────────

class _Client:
    def __init__(self, msgs=None):
        self.msgs = msgs or []

    async def get_messages(self, peer, limit=10, min_id=0):
        assert peer == al.SERVICE_ID

        class M:
            def __init__(s, d):
                s.id, s.message = d["id"], d["text"]
                from datetime import datetime, timezone
                s.date = datetime.fromtimestamp(d["date"], tz=timezone.utc)
        return [M(d) for d in self.msgs if d["id"] > min_id]


def _patch_password(monkeypatch, pw, declined):
    async def _read(client):
        return {**pw, "_raw": object()}

    async def _decline(client):
        declined.append(True)
        return True

    monkeypatch.setattr(al, "read_password", _read)
    monkeypatch.setattr(al, "decline_reset", _decline)


def test_reset_is_declined_by_default(monkeypatch):
    declined: list = []
    _patch_password(monkeypatch, {"has_password": True, "pending_reset": NOW}, declined)
    out = asyncio.run(al.inspect(_Client(), {"id": 5}, {}, ash.ShieldConfig(), []))
    assert declined == [True] and out["declined"] is True
    assert ash.ShieldConfig().guard_reset is True and ash.ShieldConfig().guard_twofa is True


def test_reset_is_left_alone_when_owner_turned_it_off(monkeypatch):
    declined: list = []
    _patch_password(monkeypatch, {"has_password": True, "pending_reset": NOW}, declined)
    out = asyncio.run(al.inspect(_Client(), {"id": 5}, {},
                                 ash.ShieldConfig(guard_reset=False), []))
    assert declined == [] and out["declined"] is False
    assert [e["kind"] for e in out["events"]] == [al.KIND_RESET_REQUESTED]


def test_inspect_remembers_the_last_service_message(monkeypatch):
    _patch_password(monkeypatch, {"has_password": True, "pending_reset": 0}, [])
    now = int(time.time())
    out = asyncio.run(al.inspect(_Client([_msg(41, now - 60)]),
                                 {"id": 5, "service_seen_msg_id": 40}, {}, None, []))
    assert out["seen_id"] == 41
    assert [e["kind"] for e in out["events"]] == [al.KIND_LOGIN_ATTEMPT]


def test_our_reserve_login_silences_its_own_code(monkeypatch):
    """Запасной ключ входит кодом — этот код не «попытка угона»."""
    _patch_password(monkeypatch, {"has_password": True, "pending_reset": 0}, [])
    now = int(time.time())
    auths = [{"hash": 99, "created": now - 30}]
    out = asyncio.run(al.inspect(_Client([_msg(41, now - 60)]),
                                 {"id": 5, "service_seen_msg_id": 40}, {}, None, auths))
    assert out["events"] == []


# ── Запись и тревога ─────────────────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def test_alert_goes_out_once_with_a_button(monkeypatch):
    from database import db
    sent, keys = [], set()

    async def _ok(pool, owner, key, cooldown):
        if key in keys:
            return False
        keys.add(key)
        return True

    async def _notify(pool, bot, owner, pref, text, reply_markup=None, *, dedup_key=None):
        sent.append((text, reply_markup))
        return True

    monkeypatch.setattr(db, "notify_dedup_ok", _ok)
    monkeypatch.setattr(db, "notify_if_enabled", _notify)
    found = {"events": [{"kind": al.KIND_RESET_REQUESTED, "until": NOW}],
             "seen_id": 41, "declined": False, "twofa_on": True, "pw_read": True}
    acc = {"id": 5, "owner_id": 7, "username": "acc5"}
    pool = _Pool()
    asyncio.run(al.record(pool, acc, found, bot=object()))
    asyncio.run(al.record(pool, acc, found, bot=object()))
    assert len(sent) == 1, "одна и та же тревога пришла дважды"
    text, kb = sent[0]
    assert "сбрасывает пароль" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith("asec:decline:5") for d in datas)
    # Отметка «разобрано» и журнал.
    upd = [a for s, a in pool.executed if "service_seen_msg_id" in s][0]
    assert upd[2] == 41 and upd[3] is not None
    ev = [a for s, a in pool.executed if "account_security_events" in s][0]
    assert ev[2] == al.KIND_RESET_REQUESTED and json.loads(ev[3])["until"] == NOW


def test_declined_reset_is_logged_as_declined(monkeypatch):
    from database import db

    async def _ok(*a, **k):
        return True

    monkeypatch.setattr(db, "notify_dedup_ok", _ok)
    monkeypatch.setattr(db, "notify_if_enabled", _ok)
    pool = _Pool()
    asyncio.run(al.record(pool, {"id": 5, "owner_id": 7},
                          {"events": [{"kind": al.KIND_RESET_REQUESTED, "until": NOW}],
                           "declined": True, "pw_read": True}, bot=object()))
    upd = [a for s, a in pool.executed if "service_seen_msg_id" in s][0]
    assert upd[3] is None, "отменённый сброс не должен висеть на карточке"
    ev = [a for s, a in pool.executed if "account_security_events" in s][0]
    assert ev[2] == al.KIND_RESET_DECLINED


def test_close_foreign_keeps_mine_and_the_reserve(monkeypatch):
    from database import db
    from services import account_manager as am
    from services import profile_setter_engine as pse
    from services import session_watch as sw

    async def _acc(pool, acc_id, owner):
        return {"id": acc_id, "owner_id": owner, "session_str": "s" * 40}

    async def _connect(sess, acc):
        return object()

    async def _disc(client):
        return None

    async def _snap(client):
        return ([{"hash": 11}, {"hash": 22}, {"hash": 33}], NOW - 3 * 86400)

    closed = []

    async def _close(client, h):
        closed.append(h)

    class P(_Pool):
        async def fetchrow(self, sql, *args):
            return {"trusted_session_hashes": [22], "reserve_auth_hash": 33}

    monkeypatch.setattr(db, "get_account_for_telethon", _acc)
    monkeypatch.setattr(pse, "_connect", _connect)
    monkeypatch.setattr(pse, "_safe_disconnect", _disc)
    monkeypatch.setattr(am, "authorizations_snapshot", _snap)
    monkeypatch.setattr(sw, "_close_one", _close)
    monkeypatch.setattr(sw, "session_old_enough", lambda ts: True)
    res = asyncio.run(al.close_foreign_now(P(), 7, 5))
    assert res["ok"] and closed == [11], "закрыли свой сеанс владельца или запасной ключ"


# ── Проводка ─────────────────────────────────────────────────────────────────

def test_sweep_runs_the_alarm_on_the_same_connection():
    src = _src("services/account_manager.py")
    assert "_alarm.inspect(" in src and "_alarm.record(" in src
    # До того, как из списка уберут запасной ключ: его вход гасит его же код.
    assert src.index("_alarm.inspect(") < src.index("_sw.drop_own_reserve(profile, _acc)")


def test_owner_read_code_is_noted_and_buttons_reach_the_screen():
    acc = _src("services/mini_app_accounts.py")
    assert "_alarm.note_code_read(pool, uid, acc_id)" in acc
    assert "/security/{action}" in acc
    js = _src("mini_app/screens/accounts_list.js")
    assert "/security/close_foreign" in js and "function accSecAction" in js
    html = _src("mini_app/index.html")
    assert "shieldToggle('guard_reset')" in html and "accSecAction(" in html
    assert "toggle_rs" in _src("bot/handlers/account_shield_hub.py")


def test_migration_turns_password_guard_on_for_everyone():
    sql = _src("schema_v379_account_alarm.sql")
    assert "guard_reset BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "UPDATE shield_configs SET guard_twofa = TRUE" in sql
