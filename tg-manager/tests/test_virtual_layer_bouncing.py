"""Невидимый триггер: «заходил и уходил».

Telegram такого события не шлёт и прислать не может: он знает отдельные
сообщения, а не то, что человек третий раз подходит к покупке и отходит. Это
состояние вычисляется только по истории переходов — ровно то, ради чего слой и
строился.

Практическая разница: «интерес» и «третий круг» выглядят одинаково по текущему
значению, а действовать надо по-разному. Ещё одно такое же письмо человеку на
третьем круге даст тот же круг.
"""
from __future__ import annotations

import inspect

import pytest

from services import virtual_layer as vl


class _Pool:
    def __init__(self, candidates, history, states=None):
        self.candidates = candidates
        self.history = history
        self.states = states or {}
        self.writes: list[tuple] = []
        self.queries: list[str] = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return self.candidates if "HAVING" in sql else self.history

    async def fetchrow(self, sql, *args):
        # get_state(owner, entity_type, entity_id, state_key)
        return self.states.get(str(args[2])) if len(args) > 2 else None

    async def execute(self, sql, *args):
        self.writes.append(args)


def _h(eid, a, b):
    return {"entity_id": eid, "from_value": a, "to_value": b, "created_at": None}


_BOUNCER = [
    _h("c-1", None, "curious"), _h("c-1", "curious", "interested"),
    _h("c-1", "interested", "curious"), _h("c-1", "curious", "interested"),
]
_CLIMBER = [
    _h("c-2", None, "curious"), _h("c-2", "curious", "interested"),
    _h("c-2", "interested", "ready"),
]


async def test_bouncer_is_found_and_climber_is_not(monkeypatch):
    pool = _Pool([{"entity_id": "c-1"}, {"entity_id": "c-2"}],
                 _BOUNCER + _CLIMBER)
    fresh = await vl.detect_bouncing(pool, 1)
    assert fresh == ["c-1"], fresh


async def test_already_marked_person_is_not_reported_again():
    """Иначе событие рождалось бы на каждом проходе и превратилось в шум."""
    pool = _Pool([{"entity_id": "c-1"}], _BOUNCER,
                 states={"c-1": {"value": "bouncing"}})
    assert await vl.detect_bouncing(pool, 1) == []
    assert pool.writes == [], "состояние переписывается без изменения"


async def test_the_rule_is_not_duplicated_in_sql():
    """Разошедшиеся копии одного правила в этом продукте уже стоили дорого."""
    src = inspect.getsource(vl.detect_bouncing)
    assert "temporal_pattern(" in src, "рисунок считает уже не общая функция"
    for word in ("FILTER (WHERE", "array_position"):
        assert word not in src, (
            f"правило рисунка переписано в SQL ({word}) — копии разъедутся")


async def test_history_is_fetched_in_one_query_for_everyone():
    """Запрос на каждого кандидата уже однажды сделал экран неоткрываемым."""
    pool = _Pool([{"entity_id": "c-1"}, {"entity_id": "c-2"}],
                 _BOUNCER + _CLIMBER)
    await vl.detect_bouncing(pool, 1)
    assert len(pool.queries) == 2, pool.queries
    assert "ANY($4::text[])" in pool.queries[1]


async def test_no_candidates_means_no_work():
    pool = _Pool([], [])
    assert await vl.detect_bouncing(pool, 1) == []
    assert len(pool.queries) == 1


async def test_broken_database_is_survived():
    class _Broken:
        async def fetch(self, *a):
            raise RuntimeError("база лежит")

    assert await vl.detect_bouncing(_Broken(), 1) == []


def test_event_reaches_the_feed():
    assert vl.BOUNCING_EVENT in vl.VIRTUAL_EVENT_KINDS, (
        "событие родится, но в ленте слоя владелец его не увидит")
    label = vl.EVENT_LABEL[vl.BOUNCING_EVENT]
    assert label and not any("a" <= c.lower() <= "z" for c in label), label


def test_marker_lives_in_its_own_key():
    """Иначе отметка затёрла бы состояние воронки."""
    assert vl.PATTERN_KEY != "funnel"
    src = inspect.getsource(vl.detect_bouncing)
    assert "PATTERN_KEY" in src


def test_organism_runs_the_detector():
    from services.organism import runner
    src = inspect.getsource(runner)
    assert "detect_bouncing" in src, "невидимый триггер не считается в проде"
    i = src.index("detect_bouncing")
    assert "except Exception" in src[i:i + 500], "сбой детектора уронит проход"


def test_brain_turns_it_into_an_action():
    from services.organism.brain import build_suggestions
    snap = {"fleet": {}, "ops": {}, "graph": {}, "vault": {},
            "vlayer": {"bouncing": 3}}
    sug = [s for s in build_suggestions(snap, now=1_000_000)
           if s["id"] == "vlayer_bouncing"]
    assert sug, "посчитали и промолчали"
    assert not any("a" <= c.lower() <= "z" for c in sug[0]["why"])

    quiet = {"fleet": {}, "ops": {}, "graph": {}, "vault": {},
             "vlayer": {"bouncing": 2}}
    assert not [s for s in build_suggestions(quiet, now=1_000_000)
                if s["id"] == "vlayer_bouncing"], "дёргаем по двум людям"


def test_snapshot_counts_them():
    from services.organism import world
    src = inspect.getsource(world._vlayer)
    assert "'bouncing'" in src and "state_key='pattern'" in src
