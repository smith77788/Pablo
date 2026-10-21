"""Полное удаление и проверка живости ботов и каналов/чатов (v253).

РАЗРЫВ (жалоба владельца): «нет механизма, который проверяет, не удалён ли
канал/чат/бот/профиль или не заблокирован ли. И нет возможности полностью
удалить из системы — есть только пауза в работе ботов».

Что было на самом деле:

* бот: «удалить» (DELETE /bot/{id}) и «пауза» (PUT …/toggle) писали ОДИН флаг
  is_active, поэтому удалённый бот оставался в списке мини-аппа, а кнопка
  «Вернуть в работу» поднимала его обратно. Бот, удалённый в @BotFather
  (вечный 401), получал метку «токен отозван», но продолжал браться в работу;
* канал/чат: удаление было голым DELETE строки managed_channels, и следующий
  «Импорт с аккаунтов» возвращал канал обратно. Канал, удалённый в самом
  Telegram, висел в списке вечно и брался в рассылки и публикации.

Профили (аккаунты) уже покрыты: удаление есть, мёртвые статусы
(account_status.DEAD_STATUSES) исключаются из работы, сессии проверяет
account_monitor. Здесь — боты и каналы/чаты.

Тесты поведенческие там, где поведение выражается без Telegram, и проверяют
SQL там, где решает сам запрос; запросы прогоняет и живой Postgres
(test_entity_removal_postgres.py).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from database import db


def _run(coro):
    return asyncio.run(coro)


def _flat(q: str) -> str:
    return " ".join(q.split())


# ── Бот: удаление ≠ пауза ─────────────────────────────────────────────────────

class _BotPool:
    def __init__(self, result="UPDATE 1"):
        self.result = result
        self.statements: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        self.statements.append((_flat(q), a))
        return self.result

    async def fetchval(self, q, *a):
        self.statements.append((_flat(q), a))
        return 0


def test_delete_bot_marks_deleted_not_just_paused():
    pool = _BotPool()
    assert _run(db.delete_bot(pool, bot_id=7, added_by=1)) is True
    q, args = pool.statements[0]
    assert "deleted_at=now()" in q, "удаление снова ничем не отличается от паузы"
    assert "is_active=FALSE" in q, "удалённый бот остаётся в работе"
    assert "DELETE FROM managed_bots" not in q, "каскадом уйдёт аудитория"
    assert args == (7, 1)


def test_deleting_twice_reports_nothing_deleted():
    pool = _BotPool(result="UPDATE 0")
    assert _run(db.delete_bot(pool, bot_id=7, added_by=1)) is False
    assert "deleted_at IS NULL" in pool.statements[0][0]


def test_bot_visibility_hides_deleted_for_every_branch():
    """Удалённый бот не виден ни своим, ни экосистеме, ни пространству."""
    from services.mini_app_api import _BOTS_VISIBLE_SQL

    sql = _flat(_BOTS_VISIBLE_SQL)
    assert sql.startswith("mb.deleted_at IS NULL AND ("), (
        "фильтр удаления не стоит перед всей группой OR")
    group = sql[len("mb.deleted_at IS NULL AND "):]
    assert group.count("(") == group.count(")"), "скобки группы OR не сходятся"
    assert group.endswith(")")


def test_bot_lists_in_the_bot_hide_deleted():
    for fn in (db.get_bots, db.get_bot):
        src = inspect.getsource(fn)
        assert "deleted_at IS NULL" in src, f"{fn.__name__} показывает удалённого бота"


def test_pause_cannot_resurrect_a_deleted_bot():
    from services import mini_app_bots

    src = inspect.getsource(mini_app_bots.setup_routes)
    toggle = src[src.index("async def toggle_bot"):src.index("async def create_funnel")]
    assert "deleted_at IS NULL" in toggle, "«Вернуть в работу» поднимает удалённого бота"
    assert "COALESCE($3::boolean, NOT is_active)" in toggle, (
        "пауза не принимает явное состояние")


def test_delete_route_deletes_instead_of_pausing():
    from services import mini_app_bots

    src = inspect.getsource(mini_app_bots.setup_routes)
    remove = src[src.index("async def bot_remove"):src.index("async def bot_webhook_info")]
    assert "delete_bot(" in remove, "DELETE /bot/{id} снова только снимает is_active"
    assert "is_active=FALSE" not in remove


@pytest.mark.parametrize("fn", [db.add_bot, db.replace_bot_token])
def test_readding_the_token_brings_a_deleted_bot_back(fn):
    """Обещание экрана удаления: «добавьте тот же токен — бот вернётся»."""
    src = inspect.getsource(fn)
    assert "deleted_at=NULL" in src, f"{fn.__name__} не снимает отметку удаления"


# ── Бот: удалён в Telegram (вечный 401) — снимается из работы ─────────────────

class _ErrPool:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        self.calls.append((_flat(q), a))
        return "UPDATE 1"


def test_dead_token_series_takes_the_bot_out_of_work():
    from services import auto_responder as ar
    from services import bot_health as bh

    pool = _ErrPool()
    _run(ar._record_bot_error(pool, 7, "Unauthorized", 401))
    q, args = pool.calls[0]
    assert "is_active = CASE WHEN $2 = $3 AND fail_streak + 1 >= $4" in q
    assert args == (7, bh.UNAUTHORIZED, bh.UNAUTHORIZED, bh.FAIL_STREAK_TO_ALERT)


def test_network_error_does_not_take_the_bot_out():
    from services import auto_responder as ar
    from services import bot_health as bh

    pool = _ErrPool()
    _run(ar._record_bot_error(pool, 7, "Connection reset", None))
    _, args = pool.calls[0]
    assert args[1] == bh.NETWORK and args[1] != args[2], (
        "временная ошибка сети снимает бота из работы")


def test_owner_hears_about_a_bot_taken_out_by_dead_token():
    from services import auto_responder as ar

    src = _flat(inspect.getsource(ar.notify_broken_bots))
    assert "is_active OR last_error = 'unauthorized'" in src, (
        "автоматически снятый бот выпал из уведомлений — владелец не узнает")
    assert "deleted_at IS NULL" in src, "уведомляем об удалённом боте"


# ── Канал/чат: удаление, которое не возвращает импорт ─────────────────────────

class _ChanConn:
    """Пул/соединение, которое исполняет ровно то, что про removed_channels и
    managed_channels сказано в запросе."""

    def __init__(self, removed: dict | None = None, rows: dict | None = None):
        self.removed = removed or {}     # channel_id -> reason
        self.rows = rows or {}           # channel_id -> dict
        self.log: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        q = _flat(q)
        self.log.append((q, a))
        if "FROM removed_channels" in q and "reason='owner'" in q:
            ids = set(a[1])
            gone = [c for c, r in self.removed.items() if r == "gone" and c in ids]
            for c in gone:
                del self.removed[c]
            return [{"channel_id": c} for c, r in self.removed.items()
                    if r == "owner" and c in ids]
        return []

    async def fetchrow(self, q, *a):
        q = _flat(q)
        self.log.append((q, a))
        if q.startswith("DELETE FROM managed_channels"):
            row = self.rows.pop(a[1], None)
            return row
        if q.startswith("DELETE FROM removed_channels"):
            if a[1] in self.removed:
                del self.removed[a[1]]
                return {"acc_id": 5, "title": "T", "username": "u",
                        "type": "channel", "access_hash": 9}
        return None

    async def execute(self, q, *a):
        q = _flat(q)
        self.log.append((q, a))
        if q.startswith("INSERT INTO removed_channels"):
            self.removed[a[1]] = a[2]
        return "OK"

    async def executemany(self, q, rows):
        for r in rows:
            self.log.append((_flat(q), r))

    def acquire(self):
        conn = self

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False
        return _Ctx()

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *e):
                return False
        return _Tx()


def _chs(*ids):
    return [{"id": i, "title": f"Ch{i}", "type": "channel"} for i in ids]


def test_import_does_not_bring_back_a_channel_the_owner_deleted():
    pool = _ChanConn(removed={2: "owner"})
    n = _run(db.add_managed_channels(pool, 1, 10, _chs(1, 2, 3)))
    written = [r[2] for q, r in pool.log if q.startswith("INSERT INTO managed_channels")]
    assert written == [1, 3], f"удалённый владельцем канал вернулся импортом: {written}"
    assert n == 2


def test_full_reimport_skips_deleted_too():
    pool = _ChanConn(removed={2: "owner"})
    _run(db.upsert_managed_channels(pool, 1, 10, _chs(1, 2), complete=True))
    written = [r[2] for q, r in pool.log if q.startswith("INSERT INTO managed_channels")]
    assert written == [1]


def test_channel_seen_again_in_dialogs_is_no_longer_marked_gone():
    """«Пропал в Telegram» снимается, как только аккаунт снова видит канал."""
    pool = _ChanConn(removed={3: "gone"})
    _run(db.add_managed_channels(pool, 1, 10, _chs(3)))
    assert 3 not in pool.removed
    written = [r[2] for q, r in pool.log if q.startswith("INSERT INTO managed_channels")]
    assert written == [3]


def test_filter_failure_never_loses_the_import():
    class _NoFetch(_ChanConn):
        async def fetch(self, q, *a):
            raise RuntimeError("relation removed_channels does not exist")

    pool = _NoFetch()
    assert _run(db.add_managed_channels(pool, 1, 10, _chs(1, 2))) == 2


def test_remove_channel_records_reason_and_stops_its_admin():
    pool = _ChanConn(rows={100: {"acc_id": 5, "title": "T", "username": "u",
                                 "type": "channel", "access_hash": 9}})
    assert _run(db.remove_managed_channel(pool, 1, 100)) is True
    assert pool.removed == {100: "owner"}
    sql = " | ".join(q for q, _ in pool.log)
    assert "UPDATE va_channel_admin SET enabled=FALSE" in sql, (
        "администратор канала продолжает публиковать в удалённый канал")


def test_remove_unknown_channel_is_reported():
    pool = _ChanConn()
    assert _run(db.remove_managed_channel(pool, 1, 100)) is False
    assert pool.removed == {}


def test_remove_rejects_unknown_reason():
    with pytest.raises(ValueError):
        _run(db.remove_managed_channel(_ChanConn(), 1, 100, reason="whatever"))


def test_restore_puts_the_same_row_back():
    pool = _ChanConn(removed={100: "owner"})
    assert _run(db.restore_removed_channel(pool, 1, 100)) is True
    ins = [a for q, a in pool.log if q.startswith("INSERT INTO managed_channels")]
    assert ins and ins[0][:3] == (1, 5, 100)
    assert 100 not in pool.removed


def test_mini_app_delete_goes_through_removal_record():
    from services import mini_app_channels

    src = inspect.getsource(mini_app_channels.setup_routes)
    body = src[src.index("async def channel_remove"):src.index("async def channels_removed")]
    assert "remove_managed_channel(" in body
    assert "DELETE FROM managed_channels" not in body, (
        "голый DELETE — следующий импорт вернёт канал")
    assert '"/api/miniapp/channels/removed"' in src
    assert '"/api/miniapp/channel/{ch_id}/restore"' in src


def test_explicit_add_clears_the_old_removal():
    from services import op_worker

    src = inspect.getsource(op_worker._exec_channel_add)
    assert "forget_removed_channels(" in src


# ── Канал/чат: проверка живости ───────────────────────────────────────────────

class _DriftPool:
    def __init__(self, channels, accounts, streak_after=1):
        self.channels = channels
        self.accounts = accounts
        self.streak_after = streak_after
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        return self.channels if "FROM managed_channels" in q else []

    async def fetchrow(self, q, *a):
        if "FROM tg_accounts" in q and "WHERE id=$1" in q:
            acc = self.accounts.get(a[0])
            return acc if acc and acc["owner_id"] == a[1] else None
        if "FROM tg_accounts" in q:
            for acc in self.accounts.values():
                if acc["owner_id"] == a[0]:
                    return acc
        return None

    async def fetchval(self, q, *a):
        self.executed.append((_flat(q), a))
        if "gone_streak = gone_streak + 1" in q:
            return self.streak_after
        return None

    async def execute(self, q, *a):
        self.executed.append((_flat(q), a))
        return "UPDATE 1"


def _channel(cid, owner_id=10, acc_id=7, access_hash=555):
    return {"id": cid, "owner_id": owner_id, "acc_id": acc_id,
            "channel_id": 1000 + cid, "title": f"Канал {cid}", "username": "",
            "about": "", "access_hash": access_hash}


def _account(aid, owner_id=10):
    return {"id": aid, "owner_id": owner_id, "session_str": f"sess-{aid}",
            "is_active": True, "acc_status": "active", "trust_score": 50}


@pytest.fixture
def _drift(monkeypatch):
    from services import account_manager, op_worker

    seen: dict = {"calls": [], "removed": [], "notified": []}
    state = {"errors": {}, "infos": {}}

    async def _fake_info(session_str, ids, **kw):
        seen["calls"].append((session_str, kw.get("known_hashes")))
        if kw.get("errors") is not None:
            kw["errors"].update(state["errors"])
        return dict(state["infos"])

    async def _true(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    async def _remove(pool, owner_id, channel_id, *, reason, detail=""):
        seen["removed"].append((owner_id, channel_id, reason))
        return True

    async def _notify(pool, bot, uid, pref, text, *a, **k):
        seen["notified"].append((uid, text, k.get("dedup_key")))
        return True

    monkeypatch.setattr(account_manager, "get_channels_full_info", _fake_info)
    monkeypatch.setattr(op_worker, "try_claim_account", _true)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(db, "remove_managed_channel", _remove)
    monkeypatch.setattr(db, "notify_if_enabled", _notify)
    seen["state"] = state
    return seen


@pytest.mark.asyncio
async def test_native_session_passes_stored_hashes(_drift):
    from services import drift_detector as dd

    pool = _DriftPool([_channel(1)], {7: _account(7)})
    await dd._check_all(pool, bot=None)
    assert _drift["calls"] == [("sess-7", {1001: 555})], (
        "удалённого канала нет в диалогах — без сохранённого хеша Telegram "
        "не скажет о нём ничего определённого")


@pytest.mark.asyncio
async def test_first_gone_answer_is_not_a_verdict(_drift):
    from services import drift_detector as dd

    _drift["state"]["errors"] = {1001: "ChannelPrivateError"}
    pool = _DriftPool([_channel(1)], {7: _account(7)}, streak_after=1)
    await dd._check_all(pool, bot=None)
    assert any("gone_streak = gone_streak + 1" in q for q, _ in pool.executed)
    assert _drift["removed"] == [], "канал убран по одной ошибке"


@pytest.mark.asyncio
async def test_confirmed_gone_channel_is_taken_out_and_owner_told(_drift):
    from services import drift_detector as dd

    _drift["state"]["errors"] = {1001: "ChannelPrivateError"}
    pool = _DriftPool([_channel(1)], {7: _account(7)},
                      streak_after=dd._GONE_CONFIRM_CHECKS)
    await dd._check_all(pool, bot=None)
    assert _drift["removed"] == [(10, 1001, db.REMOVED_GONE)]
    assert len(_drift["notified"]) == 1
    uid, text, key = _drift["notified"][0]
    assert uid == 10 and "Канал 1" in text and key == "channel_gone:10:1001"


@pytest.mark.asyncio
async def test_transient_error_is_not_counted(_drift):
    from services import drift_detector as dd

    _drift["state"]["errors"] = {1001: "TimeoutError"}
    pool = _DriftPool([_channel(1)], {7: _account(7)}, streak_after=9)
    await dd._check_all(pool, bot=None)
    assert not any("gone_streak = gone_streak + 1" in q for q, _ in pool.executed)
    assert _drift["removed"] == []


@pytest.mark.asyncio
async def test_fallback_session_never_judges_a_channel(_drift):
    """У запасного аккаунта доступа к приватному каналу может не быть вовсе."""
    from services import drift_detector as dd

    _drift["state"]["errors"] = {1001: "ChannelPrivateError"}
    # родной аккаунт 7 мёртв (нет строки) — берётся запасной 8 того же владельца
    pool = _DriftPool([_channel(1)], {8: _account(8)}, streak_after=9)
    await dd._check_all(pool, bot=None)
    assert _drift["calls"] == [("sess-8", None)]
    assert not any("gone_streak = gone_streak + 1" in q for q, _ in pool.executed)
    assert _drift["removed"] == []


@pytest.mark.asyncio
async def test_answering_channel_resets_the_counter(_drift):
    from services import drift_detector as dd

    _drift["state"]["infos"] = {1001: {"title": "Канал 1", "username": "",
                                       "about": "", "members_count": 3}}
    pool = _DriftPool([_channel(1)], {7: _account(7)})
    await dd._check_all(pool, bot=None)
    resets = [a for q, a in pool.executed if q.startswith("UPDATE managed_channels SET gone_streak=0")]
    assert resets == [([1],)]


def test_full_info_reports_errors_per_channel():
    from services import account_manager

    sig = inspect.signature(account_manager.get_channels_full_info)
    assert "errors" in sig.parameters and "known_hashes" in sig.parameters
    src = inspect.getsource(account_manager.get_channels_full_info)
    assert "errors[int(cid)] = etype" in src
