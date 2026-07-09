"""Регрессия: авто-переходы CRM-стадий аккаунта (services/stage_flow).

Чистая логика решения + инвариант «не перетирать ручные стадии». Плюс source-level
проверка, что события реально подключены в account_warmer (иначе — мёртвый код) и
db.apply_account_stage_event строит идемпотентный UPDATE со скоупом по стадиям.
"""
from __future__ import annotations

import os
import re

from services.stage_flow import (
    next_stage_on_event,
    stage_sources_for_event,
    ACCOUNT_STAGES,
    STAGE_EVENTS,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_warmup_done_only_from_raw_or_warming():
    for cur in ("", None, "new", "warming"):
        assert next_stage_on_event(cur, "warmup_done") == "ready"
    # ручные стадии не перетираются
    for cur in ("in_work", "resting", "frozen", "reserve", "ready"):
        assert next_stage_on_event(cur, "warmup_done") is None


def test_warmup_start_only_from_unmarked():
    assert next_stage_on_event("", "warmup_start") == "warming"
    assert next_stage_on_event("new", "warmup_start") == "warming"
    for cur in ("warming", "ready", "in_work", "frozen", "reserve", "resting"):
        assert next_stage_on_event(cur, "warmup_start") is None


def test_banned_freezes_everything_except_already_frozen():
    for cur in ("", "new", "warming", "ready", "in_work", "resting", "reserve"):
        assert next_stage_on_event(cur, "banned") == "frozen"
    assert next_stage_on_event("frozen", "banned") is None


def test_unknown_event_noop():
    assert next_stage_on_event("new", "explode") is None


def test_targets_are_valid_stages():
    for ev in STAGE_EVENTS:
        target, sources = stage_sources_for_event(ev)
        assert target in ACCOUNT_STAGES
        # источники — только пустая стадия или валидные стадии
        for s in sources:
            assert s == "" or s in ACCOUNT_STAGES


def test_sources_match_next_stage_decision():
    # stage_sources_for_event должен быть согласован с next_stage_on_event
    for ev in STAGE_EVENTS:
        target, sources = stage_sources_for_event(ev)
        for cur in [""] + list(ACCOUNT_STAGES):
            nxt = next_stage_on_event(cur, ev)
            if cur in sources:
                assert nxt == target
            else:
                assert nxt is None


def test_events_are_wired_into_warmup():
    with open(os.path.join(ROOT, "services/account_warmer.py"), encoding="utf-8") as f:
        src = f.read()
    for ev in ("warmup_done", "warmup_start", "banned"):
        assert f'apply_account_stage_event(pool, account_id, "{ev}")' in src \
            or f'apply_account_stage_event(pool, acc_id, "{ev}")' in src, (
            f"событие {ev} не подключено в account_warmer — мёртвая логика"
        )


def test_db_helper_idempotent_scoped_update():
    with open(os.path.join(ROOT, "database/db.py"), encoding="utf-8") as f:
        src = f.read()
    assert "async def apply_account_stage_event" in src
    # UPDATE ограничен исходными стадиями (не перетирает ручные) и ловит лаг миграции
    assert "SET stage=$2 WHERE id=$1 AND" in src
    assert "asyncpg.UndefinedColumnError" in src
