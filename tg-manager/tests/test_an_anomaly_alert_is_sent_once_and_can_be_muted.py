"""Тревога об аномалии: одна на эпизод, по свежим данным, и её можно заглушить.

Жалоба владельца (скриншот: «Аномалия обнаружена — Волна флудов: 48/50» в
10:15, 10:46, 11:17): «за час несколько раз одно и то же, я понял с первого
раза; ни отключить, ни заглушить нельзя; флот уже без ограничений, а система
всё шлёт».

Три причины, все три под регрессом:

1. Повтор. Уведомление уходило при каждой новой записи аномалии, а записи
   защищены от дублей лишь на 30 минут — отсюда шаг ровно в полчаса. Теперь
   сообщение одно на эпизод (`_is_new_episode`).
2. Устаревшие данные. Детектор «волны за последний час» считал `flood_count_7d`
   — флуды за НЕДЕЛЮ, поэтому кричал семь дней после волны на здоровом флоте.
   Теперь — журнал флудов за час.
3. Нечем заглушить. Категорию «ограничения» выключать нельзя (в ней баны и
   заморозки), «Разрешить» не помогало. Теперь под сообщением «не присылать
   24 ч / неделю» — глушится только этот тип.

Плюс текст: «Recovery Engine анализирует ситуацию…» обещал работу, которой по
аномалии не запускается, и был по-английски.
"""
from __future__ import annotations

import asyncio

from services import anomaly_detector as AD


def _run(c):
    return asyncio.run(c)


# ── 2. волна флудов по свежим данным ──────────────────────────────────────────

class _FloodPool:
    """Неделю назад была волна (flood_count_7d у 48 из 50), за последний час — тишина."""

    def __init__(self):
        self.sql: list[str] = []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        if "flood_count_7d" in sql:
            return {"accounts_with_flood": 48, "total_floods": 49, "active_total": 50}
        return {"accounts_with_flood": 0, "total_floods": 0, "active_total": 50}


def test_a_week_old_wave_is_not_a_wave_now():
    pool = _FloodPool()
    got = _run(AD._detect_flood_wave(pool, 1))
    assert got == [], "флот час без флудов, а детектор видит «волну» по недельному счётчику"
    assert any("account_flood_log" in s and "1 hour" in s for s in pool.sql)


def test_a_wave_in_the_last_hour_is_still_caught():
    class _P(_FloodPool):
        async def fetchrow(self, sql, *a):
            return {"accounts_with_flood": 45, "total_floods": 60, "active_total": 50}

    got = _run(AD._detect_flood_wave(_P(), 1))
    assert got and got[0].severity == "critical"
    assert "за последний час" in got[0].description


# ── 1 и 3. одна тревога на эпизод, и её можно заглушить ──────────────────────

class _EpisodePool:
    def __init__(self):
        self.rows: list[tuple] = []   # (owner, type, severity)
        self.muted: set[tuple] = set()

    async def fetchval(self, sql, *a):
        if "FROM anomaly_events" in sql and "severity" in sql:
            owner, typ, sev = a[0], a[1], a[2]
            return 1 if (owner, typ, sev) in self.rows else None
        if "FROM notification_dedup" in sql:
            return 1 if (a[0], a[1]) in self.muted else None
        return None

    async def fetch(self, sql, *a):
        return [{"owner_id": 7}]

    async def execute(self, sql, *a):
        if "INSERT INTO notification_dedup" in sql:
            if a[2] > 0:
                self.muted.add((a[0], a[1]))
            else:
                self.muted.discard((a[0], a[1]))
        return "OK"


def _wave():
    return AD.Anomaly(anomaly_type="flood_wave", detector="account", severity="critical",
                      title="Волна флудов: 48/50 аккаунтов", description="…", owner_id=7)


def _cycle(monkeypatch, pool, sent):
    from database import db as _db

    async def _detect(pool, owner_id):
        return [_wave()]

    async def _none(pool):
        return []

    async def _yes(pool, a):
        return True

    async def _rec(pool, a):
        pool.rows.append((a.owner_id, a.anomaly_type, a.severity))

    async def _notify(pool, bot, uid, pref, text, reply_markup=None, **k):
        sent.append((text, reply_markup))
        return True

    monkeypatch.setattr(AD, "_detect_owner", _detect)
    monkeypatch.setattr(AD, "_detect_queue_frozen", _none)
    monkeypatch.setattr(AD, "_should_record", _yes)
    monkeypatch.setattr(AD, "_record_anomaly", _rec)
    monkeypatch.setattr(_db, "notify_if_enabled", _notify)
    _run(AD.run_full_detection(pool, object()))


def test_an_ongoing_wave_is_reported_once(monkeypatch):
    pool, sent = _EpisodePool(), []
    for _ in range(3):      # три прохода детектора — 10:15, 10:46, 11:17
        _cycle(monkeypatch, pool, sent)
    assert len(sent) == 1, f"о той же волне сообщено {len(sent)} раз"


def test_the_alert_is_honest_and_has_a_mute_button(monkeypatch):
    pool, sent = _EpisodePool(), []
    _cycle(monkeypatch, pool, sent)
    text, markup = sent[0]
    assert "Recovery Engine" not in text, "обещание работы, которой не запускается"
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any("Не присылать" in b.text for b in buttons)
    assert all("mute_anomaly" in (b.callback_data or "") or "anomalies" in (b.callback_data or "")
               for b in buttons)


def test_muted_type_is_not_sent(monkeypatch):
    pool, sent = _EpisodePool(), []
    _run(AD.mute(pool, 7, "flood_wave", 24))
    _cycle(monkeypatch, pool, sent)
    assert sent == [], "заглушённая тревога всё равно пришла"
    _run(AD.mute(pool, 7, "flood_wave", 0))   # «вернуть уведомления»
    pool.rows.clear()
    _cycle(monkeypatch, pool, sent)
    assert len(sent) == 1


def test_mute_button_index_maps_to_a_known_type():
    # Номер типа живёт в callback_data: порядок списка нельзя перетасовывать.
    assert AD.ANOMALY_TYPES.index("flood_wave") == 3
    assert set(AD.ANOMALY_LABEL) == set(AD.ANOMALY_TYPES)
