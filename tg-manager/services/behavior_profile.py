"""Поведенческий профиль на аккаунт: у каждого аккаунта — свой «характер».

Проблема физики: `session_simulator.time_of_day_factor` одинаково замедляет ВСЕ
аккаунты в один и тот же час. Реальные люди разные — «жаворонки», «совы», кто-то
активнее. Если весь флот синхронно тормозит в 02:00 и синхронно оживает в 10:00 —
это сигнатура ботнета (одинаковый суточный ритм у сотни «людей»).

Здесь — детерминированный профиль из ЛИЧНОСТИ аккаунта (стабильный хэш id):
хронотип сдвигает «личную полночь» на ±несколько часов, и базовая интенсивность
слегка варьирует темп. Тот же аккаунт всегда получает тот же профиль (стабильно
между рестартами), но разные аккаунты — разные ритмы. Чистая функция, без сети,
тестируется без Telegram. Композится с гео (локальный час) и губернатором.
"""
from __future__ import annotations

import hashlib

# Хронотипы: сдвиг «личного часа» относительно реального локального и метка.
# early — встаёт/ложится раньше; late — сова; normal — обычный.
_CHRONOTYPES = (
    ("early", -3),
    ("normal", -1),
    ("normal", 0),
    ("normal", 1),
    ("late", 3),
)


def _digest(account_id) -> int:
    """Стабильное 32-битное число из id аккаунта (устойчиво между рестартами)."""
    h = hashlib.sha256(str(account_id).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def profile(account_id) -> dict:
    """Стабильный профиль аккаунта: {chronotype, hour_shift, intensity}.

    intensity ∈ [0.85, 1.15] — множитель темпа (кто-то шустрее, кто-то ленивее).
    hour_shift ∈ {-3..+3} — сдвиг суточного ритма.
    """
    d = _digest(account_id)
    chrono, shift = _CHRONOTYPES[d % len(_CHRONOTYPES)]
    # intensity: 0.85..1.15, детерминировано из другой части хэша.
    intensity = 0.85 + ((d >> 8) % 31) / 100.0
    return {"chronotype": chrono, "hour_shift": shift,
            "intensity": round(intensity, 3)}


def tod_factor(account_id, local_hour: int | None, base_factor) -> float:
    """Персональный множитель времени суток.

    `base_factor` — функция hour→множитель (обычно session_simulator.
    time_of_day_factor). Личный час = local_hour − hour_shift, поэтому «глубокая
    ночь» у совы наступает позже, чем у жаворонка. Умножаем на личную
    интенсивность. Если local_hour неизвестен (нет гео) — base_factor(None)
    (серверный час), но всё равно с личной интенсивностью.
    """
    p = profile(account_id)
    if local_hour is None:
        return base_factor(None) * p["intensity"]
    personal_hour = (int(local_hour) - p["hour_shift"]) % 24
    return base_factor(personal_hour) * p["intensity"]
