"""Virtual Layer: чистое ядро состояний и виртуальных событий.

Слой отделяет «автоответчик по событию» от модели поведения. Проверяем именно
то, чего не умеет тег: уверенность, распад по времени, каскад уровней и
рождение виртуального события ровно на реальном переходе.

Вся логика здесь — чистые функции; персистенция (signal/run_decay) проверяется
в test_virtual_layer_wiring отдельно, потому что заглушка пула не ловит типы
связывания (CLAUDE.md).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import virtual_layer as V

T0 = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _st(value, conf=0.6, exp_h=None):
    return {"value": value, "confidence": conf,
            "expires_at": (T0 + timedelta(hours=exp_h)) if exp_h is not None else None}


# ── SIGNAL: движение вверх ─────────────────────────────────────────────────

def test_signal_promotes_from_nothing():
    ch = V.apply_signal(None, "asked_price", confidence=0.7, now=T0)
    assert ch["value"] == "interested" and ch["confidence"] == 0.7


def test_signal_only_moves_up_never_down():
    """Слабый сигнал не опускает того, кто уже выше — вниз только распад."""
    cur = _st("ready", exp_h=24)
    assert V.apply_signal(cur, "opened", confidence=0.9, now=T0) is None


def test_stronger_signal_advances():
    ch = V.apply_signal(_st("curious", exp_h=100), "added_to_cart",
                        confidence=0.6, now=T0)
    assert ch["value"] == "ready"


def test_reaffirming_same_rung_builds_confidence_without_event():
    ch = V.apply_signal(_st("interested", conf=0.5, exp_h=90), "asked_price",
                        confidence=0.6, now=T0)
    assert ch["value"] == "interested"
    assert ch["confidence"] > 0.5
    assert ch["value_changed"] is False       # не переход — событие не рождаем


def test_unknown_signal_changes_nothing():
    assert V.apply_signal(_st("curious", exp_h=100), "wat", now=T0) is None


# ── SIGNAL: негатив и терминальность ───────────────────────────────────────

def test_negative_signal_sends_to_lost():
    ch = V.apply_signal(_st("ready", exp_h=24), "unsubscribed", now=T0)
    assert ch["value"] == V.LOST and ch["expires_at"] is None


def test_purchased_is_terminal_to_signals():
    assert V.apply_signal(_st("purchased"), "asked_price", now=T0) is None
    assert V.apply_signal(_st("purchased"), "opened", now=T0) is None


def test_lost_does_not_refire():
    assert V.apply_signal(_st(V.LOST), "unsubscribed", now=T0) is None


# ── Распад: тишина остужает ────────────────────────────────────────────────

def test_expired_state_decays_one_rung():
    ch = V.decay(_st("ready", conf=0.8, exp_h=-1), now=T0)   # истёк час назад
    assert ch["value"] == "qualified" and ch["confidence"] < 0.8


def test_fresh_state_does_not_decay():
    assert V.decay(_st("ready", exp_h=+5), now=T0) is None


def test_bottom_rung_has_nowhere_to_decay():
    assert V.decay(_st("new", exp_h=-1), now=T0) is None


def test_curious_decays_down_to_new():
    assert V.decay(_st("curious", exp_h=-1), now=T0)["value"] == "new"


def test_terminal_never_decays():
    assert V.decay({"value": "purchased", "confidence": 1.0,
                    "expires_at": None}, now=T0) is None


def test_hotter_states_have_shorter_memory():
    """«ready» остывает быстрее «curious» — горячий интерес живёт недолго."""
    assert V.ttl_for("ready") < V.ttl_for("curious")


# ── Виртуальные события: только на реальном переходе вверх/в LOST ──────────

def test_reaching_ready_fires_purchase_intent():
    assert V.virtual_event_for("qualified", "ready") == "purchase_intent_detected"


def test_falling_to_lost_fires_lost_interest():
    assert V.virtual_event_for("ready", V.LOST) == "user_lost_interest"


def test_decay_downwards_fires_no_event():
    """Остывание на рунг вниз (не в LOST) события не рождает — иначе шум."""
    assert V.virtual_event_for("ready", "qualified") is None


def test_no_event_without_value_change():
    assert V.virtual_event_for("interested", "interested") is None


# ── Каскад: состояния детей → состояние родителя ───────────────────────────

def test_cascade_needs_both_count_and_share():
    """20 из 100000 — не «горячая» кампания, даже если их 20."""
    kids = ["ready"] * 20 + ["new"] * 99980
    assert V.cascade(kids, min_count=20, min_share=0.15) is None


def test_cascade_hot_when_count_and_share_met():
    kids = ["ready"] * 30 + ["new"] * 70
    assert V.cascade(kids, min_count=20, min_share=0.15) == "hot"


def test_cascade_warm_below_hot():
    kids = ["ready"] * 12 + ["new"] * 88
    assert V.cascade(kids, min_count=20, min_share=0.15) == "warm"


def test_cascade_empty_is_none():
    assert V.cascade([]) is None


def test_cascade_counts_higher_rungs_too():
    """purchased тоже «горячий» — он выше порога ready."""
    kids = ["purchased"] * 25 + ["new"] * 75
    assert V.cascade(kids, hot_at="ready", min_count=20, min_share=0.15) == "hot"


# ── Ранги и устойчивость ───────────────────────────────────────────────────

def test_rank_orders_the_ladder():
    assert V.rank("new") < V.rank("interested") < V.rank("purchased")


def test_rank_of_garbage_is_negative():
    assert V.rank("что-то") == -1 and V.rank(None) == -1


def test_naive_datetime_does_not_crash_decay():
    """Из БД expires_at приходит без таймзоны — сравнение с aware упало бы."""
    naive = {"value": "ready", "confidence": 0.8,
             "expires_at": (T0 - timedelta(hours=1)).replace(tzinfo=None)}
    assert V.decay(naive, now=T0)["value"] == "qualified"
