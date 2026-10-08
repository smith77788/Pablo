"""Бан, найденный самим продуктом, должен становиться риск-сигналом.

ЧТО БЫЛО. `restriction_events` читают четыре предохранителя: гейт операций
`is_account_quarantined`, риск-пульс `get_account_health`, `anti_storm` (волна
банов → глубокий сон флота) и мониторинг. А писали в неё только два детектора:
`shadowban_monitor` (падение позиции бота, высокий флуд) и `drift_detector`
(переименование канала). Собственные находки бана — `acc_status='banned'` от
исполнителя операций — в таблицу не попадали ВООБЩЕ.

Самое дорогое следствие: `anti_storm` объявляет шторм по числу РАЗНЫХ
аккаунтов с критическим ограничением за 30 минут, считая их по этой таблице, и
зовётся ровно из хука бана `op_worker._on_account_banned`. Telegram выкашивал
двадцать аккаунтов за десять минут, хук срабатывал двадцать раз — и каждый раз
видел ноль. Предохранитель против волны банов не мог сработать на бане никогда,
и флот продолжал жечь остальные аккаунты в самой чистке.

ЧТО ТЕПЕРЬ. Хук записывает бан через `infra_memory.record_account_restriction`
ДО того, как позвать Anti-Storm, поэтому бан, из-за которого его позвали,
попадает в счёт окна. Мёртвая сессия приходит в тот же хук, но критическим
сигналом не становится: волна своих же отозванных сессий — не чистка Telegram.
"""
from __future__ import annotations

import inspect

from services import infra_memory, op_worker


class _RecordingPool:
    """Пул, который запоминает SQL и аргументы и ничего не исполняет."""

    def __init__(self, result="INSERT 0 1"):
        self.calls: list[tuple] = []
        self.result = result

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self.result

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 0

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return None


# ── Производитель сигнала ──────────────────────────────────────────────────

async def test_restriction_is_written_with_account_and_severity():
    pool = _RecordingPool()
    assert await infra_memory.record_account_restriction(
        pool, 500, 42, "ban_detected", details={"where": "invite"}) is True
    sql, args = pool.calls[0]
    assert "INSERT INTO restriction_events" in sql
    assert args[0] == 500 and args[1] == 42
    assert args[2] == "ban_detected" and args[3] == "critical"


async def test_repeat_within_the_window_is_not_written_twice():
    """Хук бана дёргается на каждой операции, налетевшей на этот аккаунт."""
    pool = _RecordingPool(result="INSERT 0 0")
    assert await infra_memory.record_account_restriction(
        pool, 500, 42, "ban_detected") is False
    sql, args = pool.calls[0]
    assert "NOT EXISTS" in sql, "дедупа нет — таблица распухнет на каждой операции"
    assert args[5] == 60, "окно дедупа не передано"


async def test_recorder_never_breaks_the_caller():
    class _Boom:
        async def execute(self, *a):
            raise RuntimeError("БД недоступна")

    assert await infra_memory.record_account_restriction(
        _Boom(), 500, 42, "ban_detected") is False


async def test_nothing_is_written_without_an_account():
    pool = _RecordingPool()
    assert await infra_memory.record_account_restriction(
        pool, 500, 0, "ban_detected") is False
    assert pool.calls == []


# ── Гейт операций видит бан ────────────────────────────────────────────────

def test_quarantine_gate_matches_the_event_type_the_hook_writes():
    """Иначе запись есть, а гейт её не узнаёт — и это снова два мира."""
    gate = inspect.getsource(infra_memory.is_account_quarantined)
    hook = inspect.getsource(op_worker._on_account_banned)
    assert 'event_type: str = "ban_detected"' in hook
    assert "severity='critical'" in gate or "%ban%" in gate


# ── Хук бана ───────────────────────────────────────────────────────────────

async def test_hook_records_the_signal_before_arming_anti_storm(monkeypatch):
    """Порядок важен: шторм считает окно, и этот бан обязан быть в счёте."""
    order: list[str] = []

    async def _rec(pool, owner_id, account_id, event_type, **kw):
        order.append(f"record:{event_type}:{kw.get('severity')}")
        return True

    async def _arm(pool, owner_id, *, bot=None):
        order.append("anti_storm")
        return {"level": "calm", "mult": 1.0}

    async def _emit(pool, owner_id, kind, payload=None):
        order.append(f"emit:{kind}")

    monkeypatch.setattr(infra_memory, "record_account_restriction", _rec)
    from services import anti_storm
    from services.organism import spine
    monkeypatch.setattr(anti_storm, "check_and_arm", _arm)
    monkeypatch.setattr(spine, "emit", _emit)

    await op_worker._on_account_banned(_RecordingPool(), 500, 42, "invite")
    assert order == ["record:ban_detected:critical", "emit:ban", "anti_storm"], order


async def test_dead_session_is_not_a_critical_ban_signal(monkeypatch):
    """Волна отозванных сессий — не чистка Telegram: усыплять флот нечего."""
    seen: list[tuple] = []

    async def _rec(pool, owner_id, account_id, event_type, **kw):
        seen.append((event_type, kw.get("severity")))
        return True

    async def _arm(pool, owner_id, *, bot=None):
        return {"level": "calm", "mult": 1.0}

    async def _emit(pool, owner_id, kind, payload=None):
        return None

    monkeypatch.setattr(infra_memory, "record_account_restriction", _rec)
    from services import anti_storm
    from services.organism import spine
    monkeypatch.setattr(anti_storm, "check_and_arm", _arm)
    monkeypatch.setattr(spine, "emit", _emit)

    await op_worker._on_account_banned(
        _RecordingPool(), 500, 42, "bulk_dm_adhoc",
        event_type="session_dead", severity="warning")
    assert seen == [("session_dead", "warning")], seen
    # И такой сигнал гейт карантина не считает серьёзным.
    gate = inspect.getsource(infra_memory.is_account_quarantined)
    assert "session_dead" not in gate


def test_dead_session_call_site_says_so():
    """Иначе мёртвая сессия пишется критическим баном и армирует шторм."""
    src = inspect.getsource(op_worker)
    i = src.index('"bulk_dm_adhoc", bot=bot')
    seg = src[i:i + 200]
    assert 'event_type="session_dead"' in seg, (
        "вызов хука для мёртвой сессии не отличён от бана")
