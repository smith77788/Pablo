"""Темп флота и осторожность аккаунта — общие для процессов и переживают рестарт.

ЗАЧЕМ. И множитель темпа (`pacing_engine`), и выученная осторожность по
аккаунту (`flood_engine`: серия флудов, риск, паузы по действиям) жили только
в памяти исполнителя. Деплой обнулял их: первые действия после выпуска шли
базовым темпом по флоту, который минуту назад ловил флуды. Второй процесс
не видел флудов соседа вовсе и темп по флоту складывался.

Здесь «процесс» — это отдельный экземпляр движка темпа и чистое состояние
flood_engine, как после рестарта. Связь между ними — только Postgres через
`services/pacing_store.py`, ровно как между двумя настоящими процессами.

Нужен живой Postgres (INFRAGRAM_TEST_DSN); без него файл пропускается.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ACC = 990_000_771

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture()
def pool():
    import asyncpg

    async def _make():
        # Пул создаётся ВНУТРИ цикла: asyncpg.create_pool(...) привязывается к
        # текущему циклу уже при вызове, до await.
        return await asyncpg.create_pool(DSN, min_size=1, max_size=3)

    p = _run(_make())
    _run(p.execute("DELETE FROM fleet_pacing_events"))
    _run(p.execute("DELETE FROM account_pacing_state WHERE account_id = $1", ACC))
    yield p
    _run(p.execute("DELETE FROM fleet_pacing_events"))
    _run(p.execute("DELETE FROM account_pacing_state WHERE account_id = $1", ACC))
    _run(p.close())


@pytest.fixture()
def fresh_process(monkeypatch):
    """Чистая память движков — как у только что поднятого процесса."""
    import services.pacing_engine as pe
    from services import flood_engine as fe

    def _new():
        monkeypatch.setattr(pe, "_engine", pe.PacingEngine())
        fe._flood_state.pop(ACC, None)
        fe._hydrated.discard(ACC)
        fe._hydrated_at.pop(ACC, None)
        fe._dirty.discard(ACC)
        return pe.get_pacing_engine()

    yield _new
    fe._flood_state.pop(ACC, None)
    fe._hydrated.discard(ACC)
    fe._hydrated_at.pop(ACC, None)
    fe._dirty.discard(ACC)


def test_floods_seen_by_one_process_slow_down_the_other(pool, fresh_process):
    from services import pacing_store

    a = fresh_process()
    for _ in range(20):
        a.record_result(success=False, is_flood=True, action_type="invite")
    assert a.get_multiplier("invite") >= 2.5
    _run(pacing_store.sync_once(pool, "proc-a"))

    b = fresh_process()          # соседний процесс: своих исходов нет
    assert b.get_multiplier("invite") == 1.0, "контроль: без обмена сосед слеп"
    _run(pacing_store.sync_once(pool, "proc-b"))
    assert b.get_multiplier("invite") >= 2.5, (
        "флуды соседа не замедлили этот процесс — темп по флоту складывается")
    assert b.should_pause() is True


def test_fleet_pace_survives_a_restart(pool, fresh_process):
    from services import pacing_store

    a = fresh_process()
    for _ in range(20):
        a.record_result(success=False, is_flood=True, action_type="strike")
    _run(pacing_store.sync_once(pool, "before-deploy"))

    after = fresh_process()       # тот же процесс после деплоя
    _run(pacing_store.sync_once(pool, "after-deploy"))
    assert after.get_multiplier("strike") >= 2.5


def test_own_unsynced_floods_count_immediately(pool, fresh_process):
    """Окно из базы не должно прятать свои исходы до следующего обмена."""
    from services import pacing_store

    a = fresh_process()
    _run(pacing_store.sync_once(pool, "p"))     # окно пришло (пустое)
    for _ in range(20):
        a.record_result(success=False, is_flood=True, action_type="join")
    assert a.get_multiplier("join") >= 2.5


def test_learned_caution_of_an_account_survives_a_restart(pool, fresh_process):
    from services import flood_engine as fe
    from services import pacing_store

    fresh_process()
    base = fe.recommended_delay(ACC, "invite")
    for _ in range(3):
        _run(fe.record_flood(None, ACC, 30, action_type="invite"))
    hot = fe.get_account_state(ACC)
    assert hot.consecutive_floods == 3
    _run(pacing_store.sync_once(pool, "before-deploy"))

    fresh_process()               # рестарт: память пуста
    assert fe.get_account_state(ACC).consecutive_floods == 0
    _run(fe.hydrate_states(pool, [ACC]))
    st = fe.get_account_state(ACC)
    assert st.consecutive_floods == 3, "серия флудов потерялась на рестарте"
    assert st.risk_score > 0.5
    assert st.action_delays.get("invite", 0) > 1.0
    assert fe.recommended_delay(ACC, "invite") > base, (
        "после рестарта аккаунт пошёл базовым темпом, как будто флудов не было")


def test_account_handed_over_comes_with_the_neighbours_state(pool, fresh_process):
    """Аккаунт, который этот процесс гидрировал раньше, после работы соседа
    перечитывается (не реже раза в минуту), а не берётся из старой памяти."""
    from services import flood_engine as fe
    from services import pacing_store

    fresh_process()
    _run(fe.hydrate_states(pool, [ACC]))      # «мы» видели аккаунт чистым
    assert fe.get_account_state(ACC).consecutive_floods == 0

    # сосед поработал аккаунтом и поймал флуды — в базе
    _run(pool.execute(
        "INSERT INTO account_pacing_state(account_id, consecutive_floods, risk_score, "
        "action_delays, last_flood_at) VALUES($1, 4, 0.8, '{\"invite\": 9}'::jsonb, NOW())",
        ACC))
    # срок свежести истёк
    fe._hydrated_at[ACC] -= fe._HYDRATE_TTL_S + 1
    _run(fe.hydrate_states(pool, [ACC]))
    st = fe.get_account_state(ACC)
    assert st.consecutive_floods == 4 and st.action_delays.get("invite") == 9.0
    assert fe.recommended_delay(ACC, "invite") > fe._ACTION_BASELINES["invite"]

    _ = pacing_store  # обмен здесь не нужен: сосед писал напрямую


def test_unsynced_local_floods_are_not_overwritten_by_older_base(pool, fresh_process):
    from services import flood_engine as fe

    fresh_process()
    _run(pool.execute(
        "INSERT INTO account_pacing_state(account_id, consecutive_floods, risk_score) "
        "VALUES($1, 1, 0.1)", ACC))
    for _ in range(3):
        _run(fe.record_flood(None, ACC, 30, action_type="invite"))   # ещё не записано
    _run(fe.hydrate_states(pool, [ACC]))
    assert fe.get_account_state(ACC).consecutive_floods == 3


def test_failed_write_keeps_the_data_for_the_next_round(fresh_process):
    """Сбой базы не теряет исходы: они возвращаются в очередь."""
    from services import pacing_store

    class _Down:
        async def execute(self, *a, **k):
            raise ConnectionError("база недоступна")

    eng = fresh_process()
    eng.record_result(success=False, is_flood=True, action_type="x")
    assert _run(pacing_store.flush_events(_Down(), "p")) == 0
    assert len(eng.drain_pending()) == 1
