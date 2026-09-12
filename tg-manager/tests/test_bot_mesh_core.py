"""Bot Mesh: чистое ядро координации и — главное — защиты от петель.

Telegram поддерживает bot-to-bot, но «bot-message handling must terminate
predictably» лежит на нас. Это самая опасная часть: сеть ботов без гашения
петель складывается в лавину и флуд-бан всего флота. Поэтому проверяем каждый из
четырёх независимых предохранителей и то, что любой из них в одиночку рвёт цепь.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import bot_mesh as M

T0 = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _route(*bots):
    return [{"bot": b, "capability": f"cap{b}"} for b in bots]


def _env(**over):
    e = M.make_envelope(7, 100, _route(200, 300, 400), {"lead": 1}, now=T0)
    e.update(over)
    return e


# ── Нормальный ход ─────────────────────────────────────────────────────────

def test_new_task_targets_first_route_step():
    e = _env()
    assert M.target_of(e)["bot"] == 200
    ok, reason = M.should_process(e, now=T0)
    assert ok and reason is None


def test_advance_walks_the_route_and_grows_trace():
    e = _env()
    e2 = M.advance(e)
    assert M.target_of(e2)["bot"] == 300
    assert e2["trace"] == [100, 200] and e2["depth"] == 1


def test_route_end_is_terminal_and_marked_done():
    e = _env()
    for _ in range(3):
        e = M.advance(e)
    assert M.is_terminal(e) and e["status"] == "done"


def test_origin_is_seeded_in_trace():
    assert _env()["trace"] == [100]


# ── Предохранитель: дедлайн ────────────────────────────────────────────────

def test_expired_task_is_dropped():
    e = _env(deadline_at=T0 - timedelta(seconds=1))
    ok, reason = M.should_process(e, now=T0)
    assert not ok and reason == M.DROP_EXPIRED


def test_task_within_deadline_passes():
    ok, _ = M.should_process(_env(), now=T0 + timedelta(seconds=10))
    assert ok


# ── Предохранитель: глубина ────────────────────────────────────────────────

def test_depth_over_limit_is_dropped():
    e = _env(depth=M.MAX_DEPTH + 1)
    ok, reason = M.should_process(e, now=T0)
    assert not ok and reason == M.DROP_MAX_DEPTH


def test_depth_at_limit_still_passes():
    e = _env(depth=M.MAX_DEPTH)
    ok, _ = M.should_process(e, now=T0)
    assert ok


# ── Предохранитель: цикл в трассе ──────────────────────────────────────────

def test_bot_visited_too_often_is_a_cycle():
    # маршрут ведёт снова к 100, который уже дважды в трассе
    e = _env(route=_route(100), trace=[100, 100])
    ok, reason = M.should_process(e, now=T0)
    assert not ok and reason == M.DROP_CYCLE


def test_one_revisit_is_allowed():
    """Оркестратор может получить результат назад — один возврат законен."""
    e = _env(route=_route(100), trace=[100])
    ok, _ = M.should_process(e, now=T0)
    assert ok


# ── Предохранитель: дедуп шага ─────────────────────────────────────────────

def test_duplicate_step_is_dropped():
    e = _env()
    seen = {(e["task_id"], 0, 200)}
    ok, reason = M.should_process(e, seen_steps=seen, now=T0)
    assert not ok and reason == M.DROP_DUPLICATE


def test_same_task_different_step_is_not_a_duplicate():
    e = M.advance(_env())            # step=1, target 300
    seen = {(e["task_id"], 0, 200)}  # видели шаг 0, не шаг 1
    ok, _ = M.should_process(e, seen_steps=seen, now=T0)
    assert ok


# ── Любой предохранитель в одиночку рвёт лавину ────────────────────────────

def test_a_two_bot_ping_pong_terminates():
    """A→B→A→B… гасится: сначала циклом по трассе, в пределе — глубиной."""
    e = M.make_envelope(7, 100, _route(200), now=T0)
    steps = 0
    # эмулируем пинг-понг: маршрут всё время ведёт к «другому» боту
    while steps < 1000:
        ok, reason = M.should_process(e, now=T0)
        if not ok:
            break
        nxt_bot = 100 if M.target_of(e)["bot"] == 200 else 200
        e = M.advance(e)
        e["route"] = e["route"] + [{"bot": nxt_bot, "capability": "x"}]
        steps += 1
    assert steps <= M.MAX_DEPTH + 1, "пинг-понг не должен идти вечно"
    assert reason in (M.DROP_MAX_DEPTH, M.DROP_CYCLE)


def test_no_route_left_is_dropped():
    e = _env(step=99)
    ok, reason = M.should_process(e, now=T0)
    assert not ok and reason == M.DROP_NO_ROUTE


# ── Устойчивость ───────────────────────────────────────────────────────────

def test_naive_deadline_does_not_crash():
    e = _env(deadline_at=(T0 - timedelta(seconds=1)).replace(tzinfo=None))
    ok, reason = M.should_process(e, now=T0)
    assert not ok and reason == M.DROP_EXPIRED


def test_make_envelope_gives_unique_task_ids():
    a = M.make_envelope(7, 100, _route(200))
    b = M.make_envelope(7, 100, _route(200))
    assert a["task_id"] != b["task_id"]
