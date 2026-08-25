"""Заглушить уведомление организма на период — прямо из самого уведомления.

Жалоба пользователя: «Хранилище не пишет» приходило каждые 6 часов сутками, и
выключить это было нечем — в ЛС у нуджа не было кнопок, а в мини-аппе есть лишь
«отклонить навсегда». Человек начинает игнорировать ВСЕ уведомления, включая
важные (риск бана) — то есть шум обесценивает работающую сигнализацию.

Ключевое свойство: заглушка ТОЧЕЧНАЯ (по id подсказки), а не глобальная.
"""
from __future__ import annotations

import asyncio

from services.organism import brain, runner


class _FakePool:
    """Мини-заглушка organism_state (как в test_organism_runner)."""

    def __init__(self):
        self._state = {}

    async def fetchrow(self, sql, *a):
        if "organism_state" in sql:
            v = self._state.get((a[0], a[1]))
            return {"value": v} if v is not None else None
        return None

    async def execute(self, sql, *a):
        if "organism_state" in sql:
            self._state[(a[0], a[1])] = a[2]
        return "OK"

    async def fetch(self, sql, *a):
        return []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── чистые помощники ──────────────────────────────────────────────────────────

def test_snooze_presets_are_coherent():
    codes = [c for c, _l, _s in brain.SNOOZE_PRESETS]
    assert codes == sorted(set(codes), key=codes.index), "дубли кодов периодов"
    for code, label, secs in brain.SNOOZE_PRESETS:
        assert secs > 0 and label
        assert brain.snooze_seconds(code) == secs
        assert brain.snooze_label(code) == label
    # неизвестный код не глушит (иначе опечатка выключала бы уведомление навсегда)
    assert brain.snooze_seconds("нет-такого") == 0


def test_active_snoozes_drops_expired_and_garbage():
    now = 1000.0
    got = brain.active_snoozes(
        {"vault_off": 2000.0, "bans": 500.0, "broken": "не число", "none": None}, now)
    assert got == {"vault_off": 2000.0}          # протухшее и мусор отброшены
    assert brain.active_snoozes(None, now) == {}


# ── фильтрация подсказок ──────────────────────────────────────────────────────

_SNAP_VAULT_DEAD = {"vault": {"health": "disabled"}, "fleet": {"bans_24h": 3}}


def _ids(snap, **kw):
    return [s["id"] for s in brain.build_suggestions(snap, **kw)]


def test_snoozed_suggestion_is_hidden_until_expiry():
    now = 1000.0
    assert "vault_off" in _ids(_SNAP_VAULT_DEAD, now=now)          # без заглушки — видно
    muted = {"vault_off": now + 3600}
    assert "vault_off" not in _ids(_SNAP_VAULT_DEAD, snoozed=muted, now=now)
    # срок вышел — подсказка возвращается сама, без ручных действий
    assert "vault_off" in _ids(_SNAP_VAULT_DEAD, snoozed=muted, now=now + 7200)


def test_snooze_is_per_suggestion_not_global():
    """Заглушив «Хранилище», пользователь ОБЯЗАН продолжать получать риск бана."""
    now = 1000.0
    ids = _ids(_SNAP_VAULT_DEAD, snoozed={"vault_off": now + 3600}, now=now)
    assert "vault_off" not in ids
    assert "bans" in ids, "заглушка одной подсказки погасила остальные — это опасно"


def test_dismissed_and_snoozed_are_independent():
    now = 1000.0
    ids = _ids(_SNAP_VAULT_DEAD, dismissed=["bans"], snoozed={"vault_off": now + 10}, now=now)
    assert ids == [] or ("bans" not in ids and "vault_off" not in ids)


# ── запись заглушки ───────────────────────────────────────────────────────────

def test_snooze_writes_expiry_and_survives_reload():
    pool = _FakePool()
    until = _run(brain.snooze(pool, 7, "vault_off", "24h"))
    assert until > 0
    from services.organism import spine
    stored = _run(spine.state_get(pool, 7, brain.SNOOZE_KEY, {}))
    assert "vault_off" in stored
    assert abs(float(stored["vault_off"]) - until) < 1.0


def test_snooze_rejects_unknown_period():
    pool = _FakePool()
    assert _run(brain.snooze(pool, 7, "vault_off", "мусор")) == 0.0
    from services.organism import spine
    assert _run(spine.state_get(pool, 7, brain.SNOOZE_KEY, {})) in ({}, None)


def test_snooze_prunes_expired_entries_on_write():
    """Словарь заглушек не должен расти вечно."""
    import time
    pool = _FakePool()
    from services.organism import spine
    _run(spine.state_set(pool, 7, brain.SNOOZE_KEY,
                         {"old_a": time.time() - 100, "old_b": time.time() - 5}))
    _run(brain.snooze(pool, 7, "vault_off", "6h"))
    stored = _run(spine.state_get(pool, 7, brain.SNOOZE_KEY, {}))
    assert set(stored) == {"vault_off"}, f"протухшие записи не вычищены: {stored}"


# ── путь до пользователя: кнопки под нуджем ───────────────────────────────────

def test_nudge_carries_snooze_buttons():
    """Без кнопок заглушить нечем — это и была суть жалобы."""
    kb = runner._snooze_kb("vault_off")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(texts) == len(brain.SNOOZE_PRESETS) + 1, "нет кнопок периодов или «навсегда»"
    for _code, label, _s in brain.SNOOZE_PRESETS:
        assert any(label in t for t in texts), f"нет кнопки на период «{label}»"
    assert all("vault_off" in d for d in datas), "кнопка не несёт id подсказки"


def test_runner_attaches_keyboard_to_real_send(monkeypatch):
    """Клавиатура должна уходить в реальный send_message, а не только существовать."""
    async def fake_pulse(pool, owner_id):
        return {"narrative": "", "snapshot": {}, "suggestions": [
            {"id": "vault_off", "severity": "urgent", "title": "Хранилище не пишет",
             "why": "переподключите", "action": {}}]}
    monkeypatch.setattr(brain, "pulse", fake_pulse)

    captured = {}

    class _Bot:
        async def send_message(self, uid, text, **kw):
            captured["uid"] = uid
            captured["kw"] = kw

    ok = _run(runner._tick_owner(_FakePool(), _Bot(), 5, now=1000.0))
    assert ok is True
    markup = captured["kw"].get("reply_markup")
    assert markup is not None, "нудж ушёл без кнопок заглушки"
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas and all("vault_off" in d for d in datas), \
        "кнопки не привязаны к той подсказке, о которой уведомили"


# ── человекочитаемый срок ─────────────────────────────────────────────────────

def test_format_until_is_human_readable():
    from bot.handlers.organism_nudge import format_until
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=3))
    ts = datetime(2026, 8, 26, 14, 30, tzinfo=tz).timestamp()
    assert format_until(ts) == "до 26.08 в 14:30"


# ── обработчик нажатия: то, что пользователь реально трогает ──────────────────

class _FakeMsg:
    def __init__(self, text="🫀 <b>Хранилище не пишет</b>\nпереподключите"):
        self.html_text = text
        self.text = text
        self.edited = None
        self.markup = None

    async def edit_text(self, text, **kw):
        self.edited = text
        self.markup = kw.get("reply_markup")

    async def answer(self, text, **kw):
        self.edited = text


class _FakeCb:
    def __init__(self, uid=42):
        self.from_user = type("U", (), {"id": uid})()
        self.message = _FakeMsg()
        self.answers = []

    async def answer(self, text="", **kw):
        self.answers.append(text)


def test_tapping_mute_button_actually_silences_next_nudge(monkeypatch):
    """Сквозной путь: нажал «6 часов» → следующий нудж не приходит, потом снова да."""
    from bot.handlers import organism_nudge as h
    from bot.callbacks import SnoozeCb

    pool = _FakePool()
    cb = _FakeCb(uid=42)
    _run(h.cb_snooze_mute(cb, SnoozeCb(action="mute", sid="vault_off", code="6h"), pool))
    assert cb.answers and "6 часов" in cb.answers[0]
    assert "Заглушено" in (cb.message.edited or ""), "пользователю не показали результат"

    # реальный build_suggestions теперь молчит про vault_off
    from services.organism import spine
    stored = _run(spine.state_get(pool, 42, brain.SNOOZE_KEY, {}))
    import time
    now = time.time()
    ids = [s["id"] for s in brain.build_suggestions(_SNAP_VAULT_DEAD, snoozed=stored, now=now)]
    assert "vault_off" not in ids
    assert "bans" in ids                       # остальное по-прежнему предупреждает
    # спустя 7 часов заглушка истекла
    later = [s["id"] for s in brain.build_suggestions(
        _SNAP_VAULT_DEAD, snoozed=stored, now=now + 7 * 3600)]
    assert "vault_off" in later


def test_never_button_is_reversible_by_unmute():
    """«Больше не напоминать» без пути назад — ловушка: человек в один тап
    навсегда теряет важный сигнал. Возврат обязан существовать."""
    from bot.handlers import organism_nudge as h
    from bot.callbacks import SnoozeCb
    from services.organism import spine

    pool = _FakePool()
    cb = _FakeCb(uid=43)
    _run(h.cb_snooze_off(cb, SnoozeCb(action="off", sid="vault_off", code="never"), pool))
    assert "vault_off" in (_run(spine.state_get(pool, 43, "dismissed", [])) or [])
    # в тексте обещан ровно тот путь возврата, который существует
    assert "/unmute" in (cb.message.edited or "")

    msg = _FakeMsg("/unmute")
    msg.from_user = type("U", (), {"id": 43})()
    _run(h.cmd_unmute(msg, pool))
    assert (_run(spine.state_get(pool, 43, "dismissed", [])) or []) == []
    assert "Вернул" in (msg.edited or "")


def test_unmute_on_clean_state_says_so():
    from bot.handlers import organism_nudge as h
    pool = _FakePool()
    msg = _FakeMsg("/unmute")
    msg.from_user = type("U", (), {"id": 44})()
    _run(h.cmd_unmute(msg, pool))
    assert "нет" in (msg.edited or "").lower()


def test_never_button_offers_immediate_undo():
    """Случайный тап «больше не напоминать» обязан отменяться на месте, а не
    только глобальной командой: иначе человек гасит важный сигнал необратимо."""
    from bot.handlers import organism_nudge as h
    from bot.callbacks import SnoozeCb
    from services.organism import spine

    pool = _FakePool()
    cb = _FakeCb(uid=51)
    _run(h.cb_snooze_off(cb, SnoozeCb(action="off", sid="vault_off", code="never"), pool))
    assert "vault_off" in (_run(spine.state_get(pool, 51, "dismissed", [])) or [])
    # на сообщении осталась кнопка отмены именно этого уведомления
    kb = cb.message.markup
    assert kb is not None, "после отключения не осталось кнопки возврата"
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any("vault_off" in d for d in datas)

    # отмена возвращает подсказку
    cb2 = _FakeCb(uid=51)
    _run(h.cb_snooze_undo(cb2, SnoozeCb(action="on", sid="vault_off", code=""), pool))
    assert (_run(spine.state_get(pool, 51, "dismissed", [])) or []) == []
    ids = [s["id"] for s in brain.build_suggestions(_SNAP_VAULT_DEAD, now=1000.0)]
    assert "vault_off" in ids


def test_undo_also_clears_a_timed_snooze():
    from bot.handlers import organism_nudge as h
    from bot.callbacks import SnoozeCb
    from services.organism import spine

    pool = _FakePool()
    _run(brain.snooze(pool, 52, "vault_off", "7d"))
    cb = _FakeCb(uid=52)
    _run(h.cb_snooze_undo(cb, SnoozeCb(action="on", sid="vault_off", code=""), pool))
    assert (_run(spine.state_get(pool, 52, brain.SNOOZE_KEY, {})) or {}) == {}
