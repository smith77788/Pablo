"""Безопасный инвайтинг: governor уровня чата.

Telegram вешает триггер на всплеск системных событий/мин в чате, замораживает
приём при аномалии, роняет лимит в «мёртвом» чате и при ранних выходах/жалобах.
Здесь проверяется, что governor это ловит: частота, заморозка, живость, стоп на
негативе, прогрессивный разогрев.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import smart_invite as si  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CFG = si.DEFAULT_CFG


def test_paused_chat_waits():
    st = {"paused_until": NOW + timedelta(minutes=10), "pause_reason": "flood"}
    d = si.decide(st, NOW, CFG)
    assert not d.allowed and d.wait_sec > 500 and not d.abort


def test_negative_ratio_aborts():
    st = {"joined_total": 20, "left_total": 8, "reported_total": 1}  # 45% негатива
    d = si.decide(st, NOW, CFG)
    assert not d.allowed and d.abort
    assert "мёртв" in d.reason or "лить дальше" in d.reason


def test_small_sample_does_not_abort():
    """На малой выборке не судим — иначе один выход остановил бы старт."""
    st = {"joined_total": 3, "left_total": 2}
    d = si.decide(st, NOW, CFG)
    assert d.allowed or not d.abort


def test_dead_chat_aborts():
    st = {"joined_total": 0, "liveness_score": 0.0}
    d = si.decide(st, NOW, CFG)
    assert not d.allowed and d.abort and "актив" in d.reason


def test_rate_limit_within_window():
    # холодный старт base_per_min=2: два уже отправлены в текущем окне → ждём
    st = {"window_start": NOW - timedelta(seconds=10), "window_count": 2,
          "joined_total": 0}
    d = si.decide(st, NOW, CFG)
    assert not d.allowed and 0 < d.wait_sec <= 60 and not d.abort


def test_allows_and_recommends_jittered_delay():
    st = {"window_start": NOW - timedelta(seconds=70), "window_count": 5,
          "joined_total": 0}   # окно старое → сбрасывается, можно
    d = si.decide(st, NOW, CFG)
    assert d.allowed and d.next_delay_sec > 0


def test_progressive_warmup_raises_limit():
    cold = si._effective_max_per_min({"joined_total": 0}, CFG)
    warm = si._effective_max_per_min({"joined_total": 50}, CFG)
    assert warm > cold, "лимит не растёт с подтверждёнными вступлениями"
    assert warm <= CFG.max_per_min_cap, "разогрев пробил потолок"


def test_low_liveness_halves_limit():
    full = si._effective_max_per_min({"joined_total": 50, "liveness_score": 1.0}, CFG)
    low = si._effective_max_per_min({"joined_total": 50, "liveness_score": 0.1}, CFG)
    assert low < full


def test_liveness_from_signals():
    dead = si.liveness_from_signals(participants=5, last_msg_age_days=400, recent_msgs=0)
    alive = si.liveness_from_signals(participants=800, last_msg_age_days=0.5, recent_msgs=40)
    assert dead < 0.2 < alive
    quiet_big = si.liveness_from_signals(participants=1_000_000, last_msg_age_days=365, recent_msgs=0)
    assert quiet_big < 0.4, "тихий миллионник всё равно рискован"


# ── живой Postgres: состояние копится по чату ────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_state_accumulates_and_freezes_on_flood():
    import asyncio
    import asyncpg

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            with open(os.path.join(ROOT, "schema_v187.sql"), encoding="utf-8") as f:
                await conn.execute(f.read())
            await conn.execute("DELETE FROM chat_invite_state WHERE owner_id=$1", 8800)

            # окно частоты растёт
            await si.note_sent(conn, 8800, "@chat")
            await si.note_sent(conn, 8800, "@chat")
            st = await si._load(conn, 8800, "@chat")
            assert st["window_count"] == 2 and st["invited_total"] == 2

            # chat_flood замораживает приём
            await si.note_outcome(conn, 8800, "@chat", "chat_flood")
            d = await si.can_invite(conn, 8800, "@chat")
            assert not d.allowed and d.wait_sec > 0, "flood не заморозил чат"

            # повторный flood — пауза длиннее (backoff)
            st1 = await si._load(conn, 8800, "@chat")
            await si.note_outcome(conn, 8800, "@chat", "chat_flood")
            st2 = await si._load(conn, 8800, "@chat")
            assert st2["flood_hits"] == 2
            assert st2["paused_until"] > st1["paused_until"]

            # живость записывается и влияет на решение
            await conn.execute("UPDATE chat_invite_state SET paused_until=NULL "
                               "WHERE owner_id=$1 AND chat_key=$2", 8800, "@chat")
            await si.set_liveness(conn, 8800, "@chat", 0.0)
            d2 = await si.can_invite(conn, 8800, "@chat")
            assert not d2.allowed and d2.abort, "мёртвый чат должен останавливать"
        finally:
            await conn.execute("DELETE FROM chat_invite_state WHERE owner_id=$1", 8800)
            await conn.close()

    asyncio.run(_run())
