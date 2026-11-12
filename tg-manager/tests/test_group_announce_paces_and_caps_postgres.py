"""Объявление в группы: темп человека, суточный потолок, метка до поста,
остановка на флуде.

НАХОДКА. `group_announce` («объявление во все группы аккаунта») шёл без
потолка вообще и с паузой в три секунды: сотня одинаковых постов за пять
минут — сигнатура, за которую Telegram даёт флуд и спам-флаг, а админы групп
банят аккаунт. Ответ Telegram «подождите N секунд» считался ошибкой одной
группы, и цикл тут же стучался в следующую — флуд продлевался, пока не кончится
список. Мёртвая сессия так же «проваливала» каждую группу по очереди. А строка
«отправлено» писалась ПОСЛЕ поста: обрыв между ними (деплой) давал на повторе
второе такое же объявление в ту же группу.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. убрать проверку `_sent_today >= _cap` -> красный
     test_daily_cap_counts_other_announces_and_defers_to_tomorrow_postgres;
  2. писать метку после поста (перенести _journal_write за post_to_channel)
     -> красный test_mark_precedes_the_post_postgres;
  3. убрать возврат на флуде -> красный test_flood_stops_the_run_and_frees_the_group_postgres;
  4. вернуть паузу 3 с -> красный test_gap_between_groups_is_human_postgres.
"""
from __future__ import annotations

import json

import pytest

from services import account_manager, op_worker
from tests.test_dm_pace_modes_e2e_postgres import _run, pool  # noqa: F401 — стенд

OWNER = 990_783
GROUPS = [-100_901, -100_902, -100_903, -100_904, -100_905]


@pytest.fixture
def stand(monkeypatch, pool):  # noqa: F811
    st = {"posted": [], "pauses": [], "answers": {}, "marks_at_post": []}

    async def _post(session, target, text, **kw):
        st["marks_at_post"].append(await pool.fetchval(
            "SELECT count(*) FROM operation_log WHERE op_id=$1 AND target=$2",
            st["op"], str(target)))
        st["posted"].append(target)
        return st["answers"].get(target, {"msg_id": 1})

    async def _dialogs(session, **kw):
        return [{"id": g, "type": "megagroup", "access_hash": 0} for g in GROUPS]

    async def _yes(*a, **k):
        return True

    async def _no(*a, **k):
        return False

    async def _none(*a, **k):
        return None

    async def _pause(pool_, op_id, seconds, where=""):
        st["pauses"].append(seconds)
        return seconds

    async def _gov(pool_, owner_id, base):
        return float(base)

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(account_manager, "get_dialogs", _dialogs)
    monkeypatch.setattr(op_worker, "try_claim_account", _yes)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _no)
    monkeypatch.setattr(op_worker, "_single_account_parked", _none)
    monkeypatch.setattr(op_worker, "cancellable_pause", _pause)
    monkeypatch.setattr(op_worker, "_governed_delay", _gov)
    op_worker._cancel_cache.clear()

    async def _seed():
        await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        acc = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name)"
            " VALUES($1,'+79960000783','sess-ga',TRUE,'active','GA') RETURNING id", OWNER)
        op = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params) "
            "VALUES($1,'group_announce','running',$2::jsonb) RETURNING id",
            OWNER, json.dumps({"acc_id": acc, "text": "Привет"}))
        return acc, op

    st["acc"], st["op"] = _run(_seed())
    return st


def _go(pool, st, **extra):  # noqa: F811
    params = {"acc_id": st["acc"], "text": "Привет", **extra}
    return _run(op_worker._exec_group_announce(pool, None, st["op"], OWNER, params))


def test_cap_defaults_and_clamps():
    assert op_worker.group_announce_cap(None) == op_worker.GROUP_ANNOUNCE_DAILY_CAP
    assert op_worker.group_announce_cap("abc") == op_worker.GROUP_ANNOUNCE_DAILY_CAP
    assert op_worker.group_announce_cap(3) == 3
    assert op_worker.group_announce_cap(10_000) == op_worker.GROUP_ANNOUNCE_DAILY_CAP_MAX
    assert op_worker.GROUP_ANNOUNCE_DAILY_CAP <= 40


def test_daily_cap_counts_other_announces_and_defers_to_tomorrow_postgres(pool, stand):  # noqa: F811
    # Утром тот же аккаунт уже разослал объявление другой операцией.
    async def _morning():
        other = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params) "
            "VALUES($1,'group_announce','done',$2::jsonb) RETURNING id",
            OWNER, json.dumps({"acc_id": stand["acc"], "text": "x"}))
        for i in range(op_worker.GROUP_ANNOUNCE_DAILY_CAP - 2):
            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status) "
                "VALUES($1,$2,$3,'ok')", other, i, f"g{i}")
    _run(_morning())
    res = _go(pool, stand)
    assert len(stand["posted"]) == 2, stand["posted"]
    assert res["status"] == "requeue" and res["defer_cause"] == "daily_cap", res
    assert res["defer_s"] > 0 and "завтра" in res["summary"]
    # Повтор назавтра (счёт обнулён) идёт дальше, а не сначала.
    _run(pool.execute("UPDATE operation_log SET created_at=now()-interval '1 day' "
                      "WHERE op_id IN (SELECT id FROM operation_queue WHERE owner_id=$1)",
                      OWNER))
    stand["posted"].clear()
    res = _go(pool, stand)
    assert stand["posted"] == GROUPS[2:], stand["posted"]
    assert res["status"] == "done"


def test_mark_precedes_the_post_postgres(pool, stand):  # noqa: F811
    _go(pool, stand)
    assert stand["posted"] == GROUPS
    assert all(n == 1 for n in stand["marks_at_post"]), (
        "объявление ушло раньше, чем группа отмечена в журнале")
    rows = _run(pool.fetch("SELECT status, message FROM operation_log WHERE op_id=$1",
                           stand["op"]))
    assert len(rows) == len(GROUPS)
    assert all(r["status"] == "ok" and r["message"] is None for r in rows)


def test_flood_stops_the_run_and_frees_the_group_postgres(pool, stand):  # noqa: F811
    stand["answers"][GROUPS[1]] = {"error": "Флуд-лимит: подождите 900с", "flood_wait": 900}
    res = _go(pool, stand)
    assert stand["posted"] == GROUPS[:2], "после флуда цикл продолжил стучаться в группы"
    assert res["status"] == "requeue" and res["defer_cause"] == "flood"
    assert res["defer_s"] >= 900
    targets = {r["target"] for r in _run(pool.fetch(
        "SELECT target FROM operation_log WHERE op_id=$1", stand["op"]))}
    assert targets == {str(GROUPS[0])}, "метка группы, куда пост не ушёл, не снята"
    stand["answers"].clear()
    stand["posted"].clear()
    _go(pool, stand)
    assert stand["posted"] == GROUPS[1:], "повтор после флуда задвоил или потерял группы"


def test_dead_session_fails_once_postgres(pool, stand):  # noqa: F811
    stand["answers"][GROUPS[0]] = {"error": "AUTH_KEY: сессия недействительна: revoked"}
    res = _go(pool, stand)
    assert stand["posted"] == GROUPS[:1]
    assert res["status"] == "failed"
    assert _run(pool.fetchval("SELECT count(*) FROM operation_log WHERE op_id=$1",
                              stand["op"])) == 0


def test_closed_group_is_an_error_not_a_stop_postgres(pool, stand):  # noqa: F811
    stand["answers"][GROUPS[0]] = {"error": "Нет прав для публикации", "banned": True}
    res = _go(pool, stand)
    assert stand["posted"] == GROUPS
    assert res["ok"] == len(GROUPS) - 1 and res["fail"] == 1
    st = _run(pool.fetchval("SELECT status FROM operation_log WHERE op_id=$1 AND target=$2",
                            stand["op"], str(GROUPS[0])))
    assert st == "error"


def test_gap_between_groups_is_human_postgres(pool, stand):  # noqa: F811
    _go(pool, stand)
    assert len(stand["pauses"]) == len(GROUPS) - 1, "пауза перед первым или после последнего"
    assert min(stand["pauses"]) >= op_worker.GROUP_ANNOUNCE_GAP_S[0]
