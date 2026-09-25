"""Температура считается по каждому боту, а не только по всей аудитории.

ЧТО БЫЛО. Каскад («много горячих детей → горячий родитель») в проде катился
только на уровень NETWORK/«аудитория»: связи «человек → бот» не существовало,
и свернуть состояния людей в состояние конкретного бота было не из чего.
Владелец видел, что аудитория греется, но не видел ГДЕ.

ЧТО ТЕПЕРЬ. Податель сигнала пишет источник в состояние ("bot_<id>"), и по
нему состояния людей группируются по ботам. Пороги ниже, чем у каскада на всю
аудиторию: у отдельного бота аудитория меньше на порядок, и порог «20 горячих»
для него недостижим — каскад не сработал бы никогда.
"""
from __future__ import annotations

import inspect

import pytest

from services import virtual_layer as vl


class _Pool:
    """Отдаёт состояния людей и записывает всё, что в него пишут."""

    def __init__(self, rows, existing=None):
        self.rows = rows
        self.existing = existing or {}
        self.writes: list[tuple] = []

    async def fetch(self, sql, *args):
        return self.rows

    async def fetchrow(self, sql, *args):
        # get_state: owner, entity_type, entity_id, state_key
        return self.existing.get(str(args[2])) if len(args) > 2 else None

    async def execute(self, sql, *args):
        self.writes.append(args)


def _people(pairs):
    return [{"source": src, "value": val} for src, val in pairs]


async def test_hot_bot_is_detected(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(vl, "_write", _noop)
    # У бота 10 — шесть «готовых» из десяти; у бота 20 — один из десяти.
    rows = _people(
        [("bot_10", "ready")] * 6 + [("bot_10", "curious")] * 4
        + [("bot_20", "ready")] + [("bot_20", "curious")] * 9)
    out = await vl.recompute_bot_cascade(_Pool(rows), 1)
    assert out.get("10") == "hot", out
    assert out.get("20") != "hot", out


async def test_thresholds_are_lower_than_for_the_whole_audience():
    """Иначе каскад по боту не сработает никогда."""
    src = inspect.getsource(vl.recompute_bot_cascade)
    assert "min_count: int = 5" in src and "min_share: float = 0.1" in src
    # Те же данные: по порогам бота — «горячо», по порогам всей аудитории — нет.
    values = ["ready"] * 6 + ["curious"] * 4
    assert vl.cascade(values, min_count=5, min_share=0.1) == "hot"
    assert vl.cascade(values, min_count=20, min_share=0.15) != "hot"


async def test_states_without_a_bot_source_are_ignored(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(vl, "_write", _noop)
    rows = _people([("intent_sensor", "ready")] * 50 + [(None, "ready")] * 50)
    assert await vl.recompute_bot_cascade(_Pool(rows), 1) == {}, (
        "состояния не от бота попали в температуру бота")


async def test_unchanged_verdict_is_not_rewritten(monkeypatch):
    written = []

    async def _spy(pool, owner_id, etype, eid, skey, change, source, from_value):
        written.append(eid)

    monkeypatch.setattr(vl, "_write", _spy)
    rows = _people([("bot_10", "ready")] * 6 + [("bot_10", "curious")] * 4)
    pool = _Pool(rows, existing={"10": {"value": "hot"}})

    async def _state(pool_, owner, etype, eid, skey):
        return {"value": "hot"} if eid == "10" else None

    monkeypatch.setattr(vl, "get_state", _state)
    out = await vl.recompute_bot_cascade(pool, 1)
    assert out.get("10") == "hot"
    assert written == [], "вердикт не изменился, а слой всё равно пишет и шумит"


async def test_empty_layer_is_quiet(monkeypatch):
    assert await vl.recompute_bot_cascade(_Pool([]), 1) == {}


def test_runner_recomputes_bot_cascade():
    """Каскад обязан считаться в проде, а не только существовать функцией."""
    from services.organism import runner
    src = inspect.getsource(runner)
    assert "recompute_bot_cascade" in src, (
        "периодический пересчёт температуры ботов пропал из организма")
    i = src.index("recompute_bot_cascade")
    seg = src[max(0, i - 400):i + 400]
    assert "except Exception" in seg, "сбой каскада может уронить проход организма"


def test_source_written_by_the_signal_matches_what_cascade_reads():
    """Формат источника — единственная связь «человек → бот»."""
    from services import auto_responder
    assert 'f"bot_{bot_id}"' in inspect.getsource(auto_responder), (
        "источник сигнала перестал называть бота — каскад «бот ← люди» ослепнет")
    assert vl._BOT_SOURCE.match("bot_12345")
    assert not vl._BOT_SOURCE.match("bot_abc")
    assert not vl._BOT_SOURCE.match("intent_sensor")


# ── Владелец должен это УВИДЕТЬ ──────────────────────────────────────────────

def test_overview_returns_bot_temperatures():
    """Посчитанное состояние, которого нет в сводке, для владельца не существует."""
    src = inspect.getsource(vl.overview)
    assert '"bots"' in src, "раздел температуры ботов пропал из сводки слоя"
    assert "TEMPERATURE_LABEL" in src, "подпись температуры не по-русски"
    assert "managed_bots" in src, (
        "бот показывается номером: имя берётся из managed_bots")


def test_temperature_labels_are_russian():
    assert set(vl.TEMPERATURE_LABEL) == {"hot", "warm"}
    for label in vl.TEMPERATURE_LABEL.values():
        assert not any("a" <= c.lower() <= "z" for c in label), label


def test_api_passes_bots_through():
    src = open("services/mini_app_api.py", encoding="utf-8").read()
    i = src.index("async def vlayer_overview(")
    seg = src[i:src.index("\n    async def ", i + 10)]
    assert '"bots": ov.get("bots")' in seg, (
        "эндпойнт слоя перестал отдавать температуру ботов")


def test_screen_renders_bot_temperatures():
    from tests.miniapp_source import miniapp_source
    ui = miniapp_source()
    i = ui.index("async function openVLayer")
    seg = ui[i:ui.index("\n}", i)]
    assert "d.bots" in seg and "Температура ботов" in seg, (
        "экран слоя не показывает, где именно греется аудитория")
