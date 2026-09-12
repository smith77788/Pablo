"""Virtual Layer: проводка — состояние пишется, событие рождается, распад идёт.

Чистое ядро проверено в test_virtual_layer_core. Здесь — стык: `signal`
записывает состояние и на РЕАЛЬНОМ переходе эмитит виртуальное событие в spine;
`run_decay` остужает просроченные; сенсор намерений кормит слой; схема доезжает
до прода; фоновый распад заведён.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

from services import virtual_layer as V
from services.organism import spine

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
RUNNER = (ROOT / "services" / "organism" / "runner.py").read_text(encoding="utf-8")
SENSOR = (ROOT / "services" / "intent_sensor.py").read_text(encoding="utf-8")


# ── Заглушка пула: хранит одно состояние и собирает историю ────────────────

class _Pool:
    def __init__(self, existing=None):
        self.state = existing            # dict|None — текущее virtual_states
        self.history = []                # записи virtual_state_history
        self.decay_rows = []             # что вернуть на выборку run_decay

    async def fetchrow(self, q, *a):
        if "FROM virtual_states" in q and "expires_at" in q:
            return self.state
        return None

    async def fetch(self, q, *a):
        if "FROM virtual_states" in q:
            return self.decay_rows
        return []

    async def execute(self, q, *a):
        if "INSERT INTO virtual_states" in q:
            self.state = {"value": a[4], "confidence": a[5], "source": a[6],
                          "expires_at": a[7]}
        elif "INSERT INTO virtual_state_history" in q:
            self.history.append({"from": a[4], "to": a[5], "reason": a[6]})
        # organism_events insert (spine.emit) — глотаем


def _run(coro):
    return asyncio.run(coro)


def setup_function(_):
    spine._clear()


# ── signal: запись + история + событие ─────────────────────────────────────

def test_signal_writes_state_and_history():
    pool = _Pool()
    ch = _run(V.signal(pool, 7, V.USER, "c1", "asked_price", confidence=0.7))
    assert ch["value"] == "interested"
    assert pool.state["value"] == "interested"
    assert pool.history[-1]["to"] == "interested"


def test_real_transition_emits_a_virtual_event():
    seen = []

    async def _h(owner_id, kind, payload, pool):
        seen.append((kind, payload["to"]))

    spine.on("purchase_intent_detected", _h)
    pool = _Pool(existing={"value": "qualified", "confidence": 0.6,
                           "source": None, "expires_at": None})
    _run(V.signal(pool, 7, V.USER, "c1", "added_to_cart", confidence=0.6))
    assert ("purchase_intent_detected", "ready") in seen


def test_reaffirm_does_not_emit_event():
    seen = []
    spine.on("*", lambda o, k, p, pool: seen.append(k) or _noop())
    pool = _Pool(existing={"value": "interested", "confidence": 0.5,
                           "source": None, "expires_at": None})
    _run(V.signal(pool, 7, V.USER, "c1", "asked_price", confidence=0.6))
    # событие перехода не рождается — только рост уверенности
    assert "user_showed_interest" not in seen


def _noop():
    return None


def test_signal_that_changes_nothing_returns_none():
    pool = _Pool(existing={"value": "ready", "confidence": 0.9,
                           "source": None, "expires_at": None})
    assert _run(V.signal(pool, 7, V.USER, "c1", "opened")) is None


# ── run_decay: остывание просроченных ──────────────────────────────────────

def test_run_decay_cools_expired_and_counts():
    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    pool = _Pool()
    pool.decay_rows = [{"owner_id": 7, "entity_type": V.USER, "entity_id": "c1",
                        "state_key": "funnel", "value": "ready", "confidence": 0.8,
                        "expires_at": past, "source": None}]
    n = _run(V.run_decay(pool))
    assert n == 1 and pool.history[-1]["to"] == "qualified"


def test_run_decay_ignores_terminal_rows():
    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    pool = _Pool()
    pool.decay_rows = [{"owner_id": 7, "entity_type": V.USER, "entity_id": "c1",
                        "state_key": "funnel", "value": "purchased",
                        "confidence": 1.0, "expires_at": past, "source": None}]
    assert _run(V.run_decay(pool)) == 0


# ── Схема, распад-цикл, кормление сенсором ─────────────────────────────────

def test_tables_mirrored_into_inline_migrations():
    assert "CREATE TABLE IF NOT EXISTS virtual_states" in API
    assert "CREATE TABLE IF NOT EXISTS virtual_state_history" in API


def test_schema_file_exists():
    assert (ROOT / "schema_v211_virtual_layer.sql").exists()


def test_decay_loop_is_wired_into_the_organism():
    assert "virtual_layer" in RUNNER and "run_decay" in RUNNER


def test_intent_sensor_feeds_the_layer():
    """Слой без источника сигналов — пустой каркас. Сенсор намерений кормит
    его CRM-стадией."""
    i = SENSOR.find("virtual_layer.signal")
    assert i != -1, "intent_sensor не подаёт сигнал в virtual_layer"
    # маппинг стадий → сигналов присутствует
    assert '"negotiation": "asked_how_to_pay"' in SENSOR
    assert '"lost": "refused"' in SENSOR


def test_intent_feed_is_fail_open():
    """Сбой слоя не должен ронять обработку входящего сообщения."""
    seg = SENSOR[SENSOR.find("virtual_layer.signal") - 300:
                 SENSOR.find("virtual_layer.signal") + 200]
    assert "try:" in seg and "except Exception" in seg


# ── Границы, зафиксированные в коде (честность концепции) ───────────────────

def test_bot_to_bot_premise_is_not_built_on():
    """Владелец отметил, что bot-to-bot как события Telegram не подтверждён.
    Модуль не должен полагаться на переписку ботов между собой."""
    src = (ROOT / "services" / "virtual_layer.py").read_text(encoding="utf-8")
    assert "bot-to-bot" in src.lower(), "граница про bot-to-bot должна быть явной"


def test_not_a_second_source_of_truth():
    """Слой — надстройка над unified_contacts.stage, не подмена."""
    src = (ROOT / "services" / "virtual_layer.py").read_text(encoding="utf-8")
    assert "unified_contacts.stage" in src
