"""Регресс: прогрев живёт по ЛОКАЛЬНОМУ времени гео аккаунта, а не по Киеву.

Первопричина: account_warmer был единственной подсистемой, оставшейся на
захардкоженном UTC+2 — множитель темпа и ночной пропуск считались по Киеву для
всего флота. Аккаунт на US-прокси «бодрствовал» в киевские часы и простаивал в
свой вечер: поведение рассинхронизировано с заявленным гео. geo_tempo уже
использовался op_worker/ghost_engine/chat_warmup — прогрев к нему подключён.
"""
from __future__ import annotations

import datetime as _dt
import os

from services import geo_tempo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


SRC = _read("services/account_warmer.py")


def test_no_kyiv_hardcode_left():
    for bad in ("kyiv_hour", "Kyiv time", "timedelta(hours=2)"):
        assert bad not in SRC, f"остался хардкод Киева: {bad}"


def test_multiplier_is_geo_and_account_aware():
    assert "def _time_of_day_multiplier(geo_country" in SRC
    assert "geo_tempo.local_factor(geo_country, account_id=account_id)" in SRC
    # темп применяется с гео плана и id аккаунта
    assert '_time_of_day_multiplier(plan.get("geo_country"), account_id=account_id)' in SRC


def test_plans_query_fetches_proxy_geo():
    assert "LEFT JOIN user_proxies up ON up.id = a.proxy_id" in SRC
    assert "up.geo_country" in SRC


def test_local_night_skip_wired_in_both_paths():
    # путь одиночных планов — фильтрация пачки
    assert 'geo_tempo.is_local_night(r["geo_country"])' in SRC
    # путь мультиаккаунтной сессии — по-аккаунтный пропуск
    assert "_gt.is_local_night(_acc_geo)" in SRC
    # объём действий считается по-аккаунтно
    assert "range(_acc_actions)" in SRC
    assert "actions_per_acc_base" in SRC


def test_session_geo_fetched_without_extra_roundtrip():
    """geo берётся тем же запросом, что и health-гейт (без N+1)."""
    i = SRC.index("Health/ban gate")
    seg = SRC[i:i + 500]
    assert "up.geo_country" in seg and "LEFT JOIN user_proxies" in seg


def test_multiplier_runs_for_known_and_unknown_geo():
    os.environ.setdefault("MANAGER_BOT_TOKEN", "x")
    os.environ.setdefault("TG_API_ID", "1")
    os.environ.setdefault("TG_API_HASH", "x")
    from services.account_warmer import _time_of_day_multiplier

    for geo in ("US", "RU", "DE", None, "ZZ"):
        v = _time_of_day_multiplier(geo)
        assert isinstance(v, float) and v > 0, f"гео={geo} дало {v}"


def test_local_hours_actually_differ_by_geo():
    """Функциональное доказательство: в один и тот же момент локальные часы
    в разных гео различаются — значит решения перестали быть общими."""
    now = _dt.datetime(2026, 8, 20, 1, 0, tzinfo=_dt.timezone.utc)  # 01:00 UTC
    h_us = geo_tempo.local_hour("US", now)
    h_ru = geo_tempo.local_hour("RU", now)
    assert h_us is not None and h_ru is not None
    assert h_us != h_ru, "часы по гео совпали — таймзоны не применяются"


def test_local_night_is_geo_dependent():
    """В 01:00 UTC у RU/UA уже ночь-утро, а у US — вечер предыдущего дня:
    глобальный «киевский» гейт останавливал бы и US-аккаунты."""
    now = _dt.datetime(2026, 8, 20, 1, 0, tzinfo=_dt.timezone.utc)
    ru_night = geo_tempo.is_local_night("RU", now)
    us_night = geo_tempo.is_local_night("US", now)
    assert ru_night != us_night, (
        f"ночь одинакова для RU({ru_night}) и US({us_night}) — гео не учитывается"
    )


def test_unknown_geo_falls_back_without_crash():
    assert geo_tempo.is_local_night(None) is False
    assert isinstance(geo_tempo.local_factor(None), float)


# ── Найдено при попытке сломать: устаревшие IANA-зоны ────────────────────────

def test_all_geo_data_timezones_resolve():
    """Каждая таймзона в geo_data обязана резолвиться.

    Регресс: geo_data хранил 'Europe/Kiev'/'Europe/Uzhgorod', которых нет в
    свежей tzdata → local_hour для УКРАИНСКИХ аккаунтов молча возвращал None, и
    вся гео-логика (ночь/темп) откатывалась на серверное время у ВСЕХ
    потребителей (op_worker, ghost_engine, chat_warmup, прогрев).
    """
    import re
    from zoneinfo import ZoneInfo

    src = _read("services/geo_data.py")
    zones = set(re.findall(r'"timezone": "([^"]+)"', src))
    assert len(zones) > 50, "датасет таймзон подозрительно мал"
    broken = []
    for z in zones:
        try:
            ZoneInfo(z)
        except Exception:
            broken.append(z)
    assert not broken, f"нерезолвящиеся таймзоны в geo_data: {broken}"


def test_ukraine_has_local_time():
    now = _dt.datetime(2026, 8, 20, 1, 0, tzinfo=_dt.timezone.utc)
    assert geo_tempo.local_hour("UA", now) is not None
    # legacy-полное имя тоже (через нормализацию geo_normalize)
    assert geo_tempo.local_hour("Ukraine", now) == geo_tempo.local_hour("UA", now)


def test_renamed_zone_alias_fallback():
    """Даже если в данные вернётся старое имя — алиас спасёт."""
    assert geo_tempo._zoneinfo("Europe/Kiev") is not None
    assert geo_tempo._zoneinfo("Asia/Calcutta") is not None
    assert geo_tempo._zoneinfo("Totally/Bogus") is None
