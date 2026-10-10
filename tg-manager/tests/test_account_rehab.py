"""Авто-реабилитация ограниченных аккаунтов: спам-блок → тихий прогрев →
перепроверка → возврат в строй.

Проверяем стейт-машину `services/account_rehab.py`: переходы фаз, возврат в строй
через set_status, бэк-офф перепроверок, уход в stuck на вечном блоке, а также
разводку (фоновый цикл в main.py, отражение фазы в пульсе флота).
"""
from __future__ import annotations

import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import account_rehab as ar  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePool:
    """Мини-пул: копит execute-вызовы, отдаёт заранее заданные fetch-строки."""

    def __init__(self):
        self.execs: list[tuple] = []

    async def execute(self, sql, *args):
        self.execs.append((sql, args))
        return "OK"

    async def fetch(self, sql, *args):
        return []

    def last_advance(self):
        """Последний UPDATE account_rehab_state (из _advance)."""
        for sql, args in reversed(self.execs):
            if "UPDATE account_rehab_state SET" in sql:
                return sql, args
        return None, None


def _advance_phase(pool):
    sql, args = pool.last_advance()
    assert sql is not None, "не было перехода фазы"
    return args[1]  # $2 = phase в _advance


# ── Чистые помощники ───────────────────────────────────────────────────────────
def test_backoff_grows_with_attempts():
    # Бэк-офф не убывает с числом попыток (частить перепроверки нельзя).
    prev = 0
    for a in range(0, 8):
        cur = ar._backoff_sec(a)
        assert cur >= 3600, "перепроверка не должна быть чаще часа"
        prev = cur
    # поздние попытки заметно реже ранних
    assert ar._backoff_sec(7) > ar._backoff_sec(0)


def test_jitter_stays_in_bounds():
    for _ in range(200):
        j = ar._jitter(1000, frac=0.3)
        assert 700 - 1 <= j <= 1300 + 1
        assert j >= 60


def test_disabled_flag(monkeypatch):
    monkeypatch.setenv("INFRAGRAM_DISABLE_REHAB", "1")
    assert ar._disabled() is True
    monkeypatch.setenv("INFRAGRAM_DISABLE_REHAB", "0")
    assert ar._disabled() is False


# ── Фаза appeal ────────────────────────────────────────────────────────────────
def test_appeal_free_returns_account_to_service(monkeypatch):
    pool = _FakePool()
    freed = {}

    async def fake_appeal(sess, acc):
        return {"ok": True, "status": "free", "reply": "", "steps": 1}

    async def fake_set_status(p, acc_id, status, **kw):
        freed["acc_id"] = acc_id
        freed["status"] = status
        return True

    monkeypatch.setattr("services.account_manager.appeal_spamblock", fake_appeal)
    monkeypatch.setattr("services.account_status.set_status", fake_set_status)

    r = {"acc_id": 501, "owner_id": 7, "phase": "appeal", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 0, "warm_cycles": 0, "kind": None}
    _run(ar._process(pool, r))

    assert freed == {"acc_id": 501, "status": "active"}, "снятый блок не вернул аккаунт в строй"
    assert _advance_phase(pool) == "freed"


def test_appeal_still_blocked_goes_to_warming(monkeypatch):
    pool = _FakePool()

    async def fake_appeal(sess, acc):
        return {"ok": True, "status": "appeal_sent", "reply": "", "steps": 1}

    monkeypatch.setattr("services.account_manager.appeal_spamblock", fake_appeal)
    monkeypatch.setattr("services.account_status.set_status",
                        lambda *a, **k: _async_true())

    r = {"acc_id": 502, "owner_id": 7, "phase": "appeal", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 0, "warm_cycles": 0, "kind": None}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "warming"


# ── Фаза warming ───────────────────────────────────────────────────────────────
def test_warming_reaches_recheck_after_enough_cycles(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(ar, "_quiet_warmup", lambda p, a: _async_val(4))

    # уже был 1 цикл прогрева → второй должен вести к перепроверке
    r = {"acc_id": 503, "owner_id": 7, "phase": "warming", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 1, "warm_cycles": ar._WARM_CYCLES_BEFORE_RECHECK - 1,
         "kind": None}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "recheck"


def test_warming_stays_warming_until_cycles_done(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(ar, "_quiet_warmup", lambda p, a: _async_val(2))
    r = {"acc_id": 504, "owner_id": 7, "phase": "warming", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 1, "warm_cycles": 0, "kind": None}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "warming"


# ── Фаза recheck ───────────────────────────────────────────────────────────────
def test_recheck_active_frees_account(monkeypatch):
    pool = _FakePool()
    freed = {}

    async def fake_check(sess, acc, check_spambot=True):
        return {"status": "active", "reason": "ok"}

    async def fake_set_status(p, acc_id, status, **kw):
        freed["acc_id"], freed["status"] = acc_id, status
        return True

    monkeypatch.setattr("services.account_manager.check_account_status_full", fake_check)
    monkeypatch.setattr("services.account_status.set_status", fake_set_status)

    r = {"acc_id": 505, "owner_id": 7, "phase": "recheck", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 1, "warm_cycles": 2, "kind": "temp"}
    _run(ar._process(pool, r))
    assert freed == {"acc_id": 505, "status": "active"}
    assert _advance_phase(pool) == "freed"


def test_recheck_still_blocked_backs_off_to_warming(monkeypatch):
    pool = _FakePool()

    async def fake_check(sess, acc, check_spambot=True):
        return {"status": "spamblock", "spamblock_kind": "temp"}

    monkeypatch.setattr("services.account_manager.check_account_status_full", fake_check)
    monkeypatch.setattr("services.account_status.set_status", lambda *a, **k: _async_true())

    r = {"acc_id": 506, "owner_id": 7, "phase": "recheck", "session_str": "x" * 20,
         "attempts": 1, "appeal_count": 1, "warm_cycles": 2, "kind": "temp"}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "warming"


def test_recheck_perm_exhausted_goes_stuck(monkeypatch):
    pool = _FakePool()

    async def fake_check(sess, acc, check_spambot=True):
        return {"status": "spamblock", "spamblock_kind": "perm"}

    monkeypatch.setattr("services.account_manager.check_account_status_full", fake_check)
    monkeypatch.setattr("services.account_status.set_status", lambda *a, **k: _async_true())

    # вечный блок, аппеляции исчерпаны, много попыток → ручной разбор
    r = {"acc_id": 507, "owner_id": 7, "phase": "recheck", "session_str": "x" * 20,
         "attempts": ar._MAX_RECHECKS, "appeal_count": ar._MAX_APPEALS,
         "warm_cycles": 2, "kind": "perm"}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "stuck"


def test_recheck_terminal_status_goes_gone(monkeypatch):
    pool = _FakePool()

    async def fake_check(sess, acc, check_spambot=True):
        return {"status": "banned", "reason": "banned"}

    monkeypatch.setattr("services.account_manager.check_account_status_full", fake_check)
    monkeypatch.setattr("services.account_status.set_status", lambda *a, **k: _async_true())

    r = {"acc_id": 508, "owner_id": 7, "phase": "recheck", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 1, "warm_cycles": 2, "kind": "temp"}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "gone"


# ── Разводка ───────────────────────────────────────────────────────────────────
def test_wired_into_main_loop_and_ddl():
    m = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "account_rehab.run_rehab_loop" in m, "цикл реабилитации не запущен"
    assert "account_rehab_state" in m, "нет defensive DDL таблицы реабилитации"


def test_pulse_shows_rehab_phase():
    fp = open(os.path.join(ROOT, "services", "fleet_pulse.py"), encoding="utf-8").read()
    assert "account_rehab_state" in fp, "пульс не читает фазу реабилитации"
    assert "_rehab_reason" in fp, "пульс не показывает человеческую фазу реабилитации"


# ── Хелперы для monkeypatch-заглушек ───────────────────────────────────────────
def _async_true():
    async def _c():
        return True
    return _c()


def _async_val(v):
    async def _c(*a, **k):
        return v
    return _c(*[], **{})


# ── Ветки ошибок в _process ────────────────────────────────────────────────────
def test_appeal_exception_still_advances_to_warming(monkeypatch):
    pool = _FakePool()

    async def boom(sess, acc):
        raise RuntimeError("сеть упала")

    monkeypatch.setattr("services.account_manager.appeal_spamblock", boom)
    monkeypatch.setattr("services.account_status.set_status", lambda *a, **k: _async_true())
    r = {"acc_id": 601, "owner_id": 7, "phase": "appeal", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 0, "warm_cycles": 0, "kind": None}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "warming"


def test_recheck_exception_backs_off_to_warming(monkeypatch):
    pool = _FakePool()

    async def boom(sess, acc, check_spambot=True):
        raise RuntimeError("таймаут @SpamBot")

    monkeypatch.setattr("services.account_manager.check_account_status_full", boom)
    monkeypatch.setattr("services.account_status.set_status", lambda *a, **k: _async_true())
    r = {"acc_id": 602, "owner_id": 7, "phase": "recheck", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 1, "warm_cycles": 2, "kind": "temp"}
    _run(ar._process(pool, r))
    assert _advance_phase(pool) == "warming"


# ── run_rehab_cycle: оркестрация и изоляция сбоев ──────────────────────────────
def test_cycle_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: True)
    assert _run(ar.run_rehab_cycle(_FakePool())) == 0


def test_cycle_processes_due_rows(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "_sync_enrollment", lambda p: _async_true())
    monkeypatch.setattr(ar, "_due_rows", lambda p, n: _async_val([{"acc_id": 1}, {"acc_id": 2}]))
    seen = []
    monkeypatch.setattr(ar, "_process", lambda p, r: _record_async(seen, r))
    # без реальных пауз между аккаунтами
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())
    n = _run(ar.run_rehab_cycle(_FakePool()))
    assert n == 2 and len(seen) == 2


def test_cycle_sync_failure_is_isolated(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)

    async def boom(p):
        raise RuntimeError("db down")
    monkeypatch.setattr(ar, "_sync_enrollment", boom)
    assert _run(ar.run_rehab_cycle(_FakePool())) == 0


def test_cycle_process_failure_does_not_abort_others(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "_sync_enrollment", lambda p: _async_true())
    monkeypatch.setattr(ar, "_due_rows", lambda p, n: _async_val([{"acc_id": 1}, {"acc_id": 2}]))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())

    async def one_fails(p, r):
        if r["acc_id"] == 1:
            raise RuntimeError("сбой на первом")
    monkeypatch.setattr(ar, "_process", one_fails)
    # первый упал (не засчитан), второй отработал → 1
    assert _run(ar.run_rehab_cycle(_FakePool())) == 1


# ── _quiet_warmup ──────────────────────────────────────────────────────────────
def test_quiet_warmup_skips_when_claim_fails(monkeypatch):
    monkeypatch.setattr("services.op_worker.try_claim_account", lambda a: _async_val(False))
    n = _run(ar._quiet_warmup(_FakePool(), {"acc_id": 9, "owner_id": 7, "session_str": "s"}))
    assert n == 0


def test_quiet_warmup_runs_passive_actions(monkeypatch):
    released = []

    class _FakeClient:
        async def disconnect(self):
            return None

    monkeypatch.setattr("services.op_worker.try_claim_account", lambda a: _async_val(True))
    monkeypatch.setattr("services.op_worker.release_accounts",
                        lambda ids: _record_async(released, ids))
    monkeypatch.setattr("database.db.get_account_for_telethon",
                        lambda pool, acc_id: _async_val({"session_str": "s" * 20}))
    monkeypatch.setattr("services.account_manager.connect_client",
                        lambda *a, **k: _async_val(_FakeClient()))
    monkeypatch.setattr("services.account_warmer._get_warmup_resources",
                        lambda pool, oid: _async_val({"channels": [{"username": "chan"}]}))
    monkeypatch.setattr("services.account_warmer._perform_update_presence",
                        lambda c: _async_val(True))
    monkeypatch.setattr("services.account_warmer._perform_read_channel",
                        lambda c, ref: _async_val(True))
    monkeypatch.setattr("services.account_warmer._perform_view_profile",
                        lambda c, ref: _async_val(True))
    monkeypatch.setattr("services.account_warmer._perform_open_chat",
                        lambda c, ref: _async_val(True))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(ar.random, "randint", lambda a, b: a)
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())

    n = _run(ar._quiet_warmup(_FakePool(), {"acc_id": 9, "owner_id": 7, "session_str": "s"}))
    assert n >= 1, "пассивные действия не выполнились"
    assert released == [[9]], "аккаунт не освобождён после прогрева"


# ── rehab_overview ─────────────────────────────────────────────────────────────
def test_rehab_overview_counts_phases():
    class _P:
        async def fetch(self, sql, *a):
            return [{"phase": "warming", "n": 2}, {"phase": "freed", "n": 3},
                    {"phase": "stuck", "n": 1}, {"phase": "recheck", "n": 1}]
    ov = _run(ar.rehab_overview(_P(), 7))
    assert ov["in_progress"] == 3   # warming + recheck (+appeal 0)
    assert ov["freed"] == 3 and ov["stuck"] == 1


def _record_async(bucket, item):
    async def _c():
        bucket.append(item)
    return _c()


def _async_raise(exc):
    async def _c(*a, **k):
        raise exc
    return _c()


def test_cycle_due_failure_is_isolated(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "_sync_enrollment", lambda p: _async_true())

    async def boom(p, n):
        raise RuntimeError("db down")
    monkeypatch.setattr(ar, "_due_rows", boom)
    assert _run(ar.run_rehab_cycle(_FakePool())) == 0


# ── run_rehab_loop ─────────────────────────────────────────────────────────────
def test_loop_disabled_returns_immediately(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: True)
    assert _run(ar.run_rehab_loop(_FakePool())) is None


def test_loop_runs_one_iteration_then_cancels(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "run_rehab_cycle", lambda p: _async_val(1))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    calls = {"n": 0}

    async def sleeper(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:            # стартовая задержка — 1-я; пост-тик — 2-я
            raise asyncio.CancelledError()

    monkeypatch.setattr(ar.asyncio, "sleep", sleeper)
    with pytest.raises(asyncio.CancelledError):
        _run(ar.run_rehab_loop(_FakePool()))
    assert calls["n"] >= 2


# ── _quiet_warmup: остальные ветки ─────────────────────────────────────────────
def test_cycle_process_cancelled_propagates(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "_sync_enrollment", lambda p: _async_true())
    monkeypatch.setattr(ar, "_due_rows", lambda p, n: _async_val([{"acc_id": 1}]))
    monkeypatch.setattr(ar, "_process", lambda p, r: _async_raise(asyncio.CancelledError()))
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())
    with pytest.raises(asyncio.CancelledError):
        _run(ar.run_rehab_cycle(_FakePool()))


def test_loop_cycle_cancelled_propagates(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "run_rehab_cycle",
                        lambda p: _async_raise(asyncio.CancelledError()))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())
    with pytest.raises(asyncio.CancelledError):
        _run(ar.run_rehab_loop(_FakePool()))


def test_loop_cycle_generic_error_is_logged_and_survives(monkeypatch):
    monkeypatch.setattr(ar, "_disabled", lambda: False)
    monkeypatch.setattr(ar, "run_rehab_cycle",
                        lambda p: _async_raise(RuntimeError("тик упал")))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    calls = {"n": 0}

    async def sleeper(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:            # старт — 1-я; пост-тик после ошибки — 2-я
            raise asyncio.CancelledError()

    monkeypatch.setattr(ar.asyncio, "sleep", sleeper)
    with pytest.raises(asyncio.CancelledError):
        _run(ar.run_rehab_loop(_FakePool()))


def test_quiet_warmup_action_cancelled_propagates(monkeypatch):
    released = []

    class _Client:
        async def disconnect(self):
            return None

    monkeypatch.setattr("services.op_worker.try_claim_account", lambda a: _async_val(True))
    monkeypatch.setattr("services.op_worker.release_accounts",
                        lambda ids: _record_async(released, ids))
    monkeypatch.setattr("database.db.get_account_for_telethon",
                        lambda pool, acc_id: _async_val({"session_str": "s" * 20}))
    monkeypatch.setattr("services.account_manager.connect_client",
                        lambda *a, **k: _async_val(_Client()))
    monkeypatch.setattr("services.account_warmer._get_warmup_resources",
                        lambda pool, oid: _async_val({"channels": []}))
    monkeypatch.setattr("services.account_warmer._perform_update_presence",
                        lambda c: _async_val(False))
    monkeypatch.setattr("services.account_warmer._perform_read_channel",
                        lambda c, ref: _async_raise(asyncio.CancelledError()))
    monkeypatch.setattr("services.account_warmer._perform_view_profile",
                        lambda c, ref: _async_raise(asyncio.CancelledError()))
    monkeypatch.setattr("services.account_warmer._perform_open_chat",
                        lambda c, ref: _async_raise(asyncio.CancelledError()))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(ar.random, "randint", lambda a, b: a)
    monkeypatch.setattr(ar.random, "shuffle", lambda seq: None)
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())
    with pytest.raises(asyncio.CancelledError):
        _run(ar._quiet_warmup(_FakePool(), {"acc_id": 9, "owner_id": 7, "session_str": "s"}))
    assert released == [[9]], "отмена не должна мешать освобождению аккаунта"


def test_quiet_warmup_no_session_returns_zero(monkeypatch):
    monkeypatch.setattr("services.op_worker.try_claim_account", lambda a: _async_val(True))
    monkeypatch.setattr("services.op_worker.release_accounts", lambda ids: _async_true())
    monkeypatch.setattr("database.db.get_account_for_telethon",
                        lambda pool, acc_id: _async_val(None))
    n = _run(ar._quiet_warmup(_FakePool(), {"acc_id": 9, "owner_id": 7, "session_str": "s"}))
    assert n == 0


def test_quiet_warmup_connect_failure_still_releases(monkeypatch):
    released = []
    monkeypatch.setattr("services.op_worker.try_claim_account", lambda a: _async_val(True))
    monkeypatch.setattr("services.op_worker.release_accounts",
                        lambda ids: _record_async(released, ids))
    monkeypatch.setattr("database.db.get_account_for_telethon",
                        lambda pool, acc_id: _async_val({"session_str": "s" * 20}))
    monkeypatch.setattr("services.account_manager.connect_client",
                        lambda *a, **k: _async_raise(RuntimeError("коннект упал")))
    n = _run(ar._quiet_warmup(_FakePool(), {"acc_id": 9, "owner_id": 7, "session_str": "s"}))
    assert n == 0 and released == [[9]]


def test_quiet_warmup_survives_action_and_cleanup_errors(monkeypatch):
    class _BadClient:
        async def disconnect(self):
            raise RuntimeError("disconnect упал")

    monkeypatch.setattr("services.op_worker.try_claim_account", lambda a: _async_val(True))
    monkeypatch.setattr("services.op_worker.release_accounts",
                        lambda ids: _async_raise(RuntimeError("release упал")))
    monkeypatch.setattr("database.db.get_account_for_telethon",
                        lambda pool, acc_id: _async_val({"session_str": "s" * 20}))
    monkeypatch.setattr("services.account_manager.connect_client",
                        lambda *a, **k: _async_val(_BadClient()))
    monkeypatch.setattr("services.account_warmer._get_warmup_resources",
                        lambda pool, oid: _async_raise(RuntimeError("ресурсы упали")))
    monkeypatch.setattr("services.account_warmer._WARMUP_PUBLIC_CHANNELS", ["@a"])
    monkeypatch.setattr("services.account_warmer._perform_update_presence",
                        lambda c: _async_raise(RuntimeError("presence упал")))
    monkeypatch.setattr("services.account_warmer._perform_read_channel",
                        lambda c, ref: _async_raise(RuntimeError("read упал")))
    monkeypatch.setattr("services.account_warmer._perform_view_profile",
                        lambda c, ref: _async_raise(RuntimeError("view упал")))
    monkeypatch.setattr("services.account_warmer._perform_open_chat",
                        lambda c, ref: _async_raise(RuntimeError("open упал")))
    monkeypatch.setattr(ar.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(ar.random, "randint", lambda a, b: b)   # n=max → сработает break
    monkeypatch.setattr(ar.random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(ar.random, "shuffle", lambda seq: None)
    monkeypatch.setattr(ar.asyncio, "sleep", lambda *a, **k: _async_true())

    # ничего не должно всплыть наружу, несмотря на сбои на каждом шаге
    n = _run(ar._quiet_warmup(_FakePool(), {"acc_id": 9, "owner_id": 7, "session_str": "s"}))
    assert n == 0


# ── Живой Postgres: enrollment + полный цикл до freed ──────────────────────────
@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_enrollment_and_full_cycle_on_real_schema(monkeypatch):
    import asyncpg

    async def _r():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS account_rehab_state ("
                "acc_id BIGINT PRIMARY KEY, owner_id BIGINT NOT NULL, "
                "phase TEXT NOT NULL DEFAULT 'appeal', kind TEXT, "
                "attempts INTEGER NOT NULL DEFAULT 0, appeal_count INTEGER NOT NULL DEFAULT 0, "
                "warm_cycles INTEGER NOT NULL DEFAULT 0, warm_actions INTEGER NOT NULL DEFAULT 0, "
                "note TEXT, first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "last_action_at TIMESTAMPTZ, next_action_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "freed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            await conn.execute("DELETE FROM account_rehab_state WHERE owner_id=$1", 9900)
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", 9900)
            await conn.execute(
                "INSERT INTO tg_accounts(id, owner_id, phone, first_name, is_active, "
                "session_str, acc_status) VALUES "
                "(99001,$1,'+1','Blocked',TRUE,'sessionsessionsession','spamblock')",
                9900)

            # 1) enrollment заводит стейт для спам-блока
            await ar._sync_enrollment(conn)
            row = await conn.fetchrow(
                "SELECT phase FROM account_rehab_state WHERE acc_id=99001")
            assert row and row["phase"] == "appeal"

            # 2) due-строки возвращают наш аккаунт
            due = await ar._due_rows(conn, 5)
            assert any(int(d["acc_id"]) == 99001 for d in due)

            # 3) снятие блока где-то ещё → enrollment закрывает как freed
            await conn.execute(
                "UPDATE tg_accounts SET acc_status='active' WHERE id=99001")
            await ar._sync_enrollment(conn)
            row = await conn.fetchrow(
                "SELECT phase, freed_at FROM account_rehab_state WHERE acc_id=99001")
            assert row["phase"] == "freed" and row["freed_at"] is not None

            # 4) overview считает freed
            ov = await ar.rehab_overview(conn, 9900)
            assert ov["freed"] >= 1
        finally:
            await conn.execute("DELETE FROM account_rehab_state WHERE owner_id=$1", 9900)
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", 9900)
            await conn.close()

    _run(_r())
