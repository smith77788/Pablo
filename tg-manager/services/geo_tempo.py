"""Гео-осведомлённый темп: ночной режим по таймзоне аккаунта, а не сервера.

`session_simulator.time_of_day_factor()` замедляет действия ночью — но по времени
СЕРВЕРА. Аккаунт на британском прокси в 3 ночи по Лондону должен «спать», даже
если на сервере полдень: всплеск активности в локальную ночь — классический
бот-сигнал. Здесь чистое сопоставление country_code → таймзона → локальный час,
чтобы физика тайминга уважала гео флота.

Карта таймзон строится из `geo_data` (единый источник гео-истины) — по стране
берём столицу/крупнейший город. Без сети, детерминировано при фиксированном
`now`, поэтому тестируется без Telegram.
"""
from __future__ import annotations

import datetime as _dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)


def _build_cc_tz() -> dict[str, str]:
    """country_code(lower) → IANA timezone, из всех гео-списков geo_data.

    Столицы имеют приоритет над обычными городами (обрабатываем EUROPE/WORLD
    CAPITALS первыми), поэтому у страны — таймзона столицы.
    """
    from services import geo_data
    mapping: dict[str, str] = {}
    # Приоритет: столицы → затем остальные списки (не перетирают уже занятое).
    ordered: list[str] = ["EUROPE_CAPITALS", "WORLD_CAPITALS"]
    for name in dir(geo_data):
        if name.isupper() and name not in ordered:
            ordered.append(name)
    for name in ordered:
        val = getattr(geo_data, name, None)
        if not isinstance(val, (list, tuple)):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            cc = (item.get("country_code") or "").strip().lower()
            tz = (item.get("timezone") or "").strip()
            if cc and tz and cc not in mapping:
                mapping[cc] = tz
    return mapping


_CC_TZ: dict[str, str] = {}


def _cc_tz() -> dict[str, str]:
    global _CC_TZ
    if not _CC_TZ:
        try:
            _CC_TZ = _build_cc_tz()
        except Exception as e:  # pragma: no cover — geo_data всегда есть
            log.debug("geo_tempo: build map failed: %s", e)
            _CC_TZ = {}
    return _CC_TZ


def timezone_for(country_code: str | None) -> str | None:
    """IANA-таймзона для country_code (регистронезависимо) или None."""
    if not country_code:
        return None
    return _cc_tz().get(country_code.strip().lower())


def local_hour(country_code: str | None,
               now: _dt.datetime | None = None) -> int | None:
    """Локальный час (0–23) в стране аккаунта. None, если гео неизвестно.

    `now` — UTC-момент (для тестов). По умолчанию текущий UTC.
    """
    tz = timezone_for(country_code)
    if not tz:
        return None
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    try:
        return now.astimezone(ZoneInfo(tz)).hour
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def local_factor(country_code: str | None,
                 now: _dt.datetime | None = None,
                 account_id=None) -> float:
    """Множитель темпа с учётом ЛОКАЛЬНОГО времени аккаунта (≥ ~0.75).

    Если гео неизвестно — откатываемся на серверное время (текущее поведение),
    чтобы ничего не сломать. Реюз кривой `session_simulator.time_of_day_factor`.

    Если передан `account_id` — накладывается персональный хронотип
    (behavior_profile): флот не тормозит синхронно в один час (анти-сигнатура
    ботнета). Без account_id — прежнее гео-поведение.
    """
    from services import session_simulator
    h = local_hour(country_code, now)
    if account_id is not None:
        from services import behavior_profile
        return behavior_profile.tod_factor(
            account_id, h, session_simulator.time_of_day_factor)
    return session_simulator.time_of_day_factor(h)  # h=None → серверный час


def is_local_night(country_code: str | None,
                   now: _dt.datetime | None = None) -> bool:
    """Сейчас у аккаунта локальная ночь (23–7)? None-гео → False (не знаем)."""
    h = local_hour(country_code, now)
    if h is None:
        return False
    return h >= 23 or h <= 6
