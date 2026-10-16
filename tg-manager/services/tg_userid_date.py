"""Оценка даты регистрации Telegram-аккаунта по его user_id.

Telegram НЕ отдаёт дату регистрации через API. Но числовые user_id выдаются
приблизительно монотонно во времени, поэтому по набору опорных точек (id → дата)
дату создания аккаунта можно ОЦЕНИТЬ линейной интерполяцией между ближайшими
опорами (и экстраполяцией по последнему сегменту для самых новых id).

Это ОЦЕНКА (погрешность — недели/месяцы), а не точная дата. В UI помечать как
«≈ примерно». Чистый модуль без внешних зависимостей — тестируется в песочнице.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Опорные точки (user_id, unix_ts UTC). Приблизительные, из открытых наблюдений
# соответствия id↔дата; отсортированы по возрастанию id. Значения консервативны
# и служат только для грубой оценки «старый/новый аккаунт».
_ANCHORS: list[tuple[int, int]] = [
    (1_000_000,    1_383_264_000),  # ≈ 2013-11-01
    (10_000_000,   1_388_448_000),  # ≈ 2013-12-31
    (100_000_000,  1_441_065_600),  # ≈ 2015-09-01
    (200_000_000,  1_504_224_000),  # ≈ 2017-09-01
    (400_000_000,  1_564_617_600),  # ≈ 2019-08-01
    (700_000_000,  1_598_918_400),  # ≈ 2020-09-01
    (1_000_000_000, 1_622_505_600),  # ≈ 2021-06-01
    (1_300_000_000, 1_638_316_800),  # ≈ 2021-12-01
    (2_000_000_000, 1_669_852_800),  # ≈ 2022-12-01
    (5_000_000_000, 1_698_796_800),  # ≈ 2023-11-01
    (6_500_000_000, 1_717_200_000),  # ≈ 2024-06-01
    (7_500_000_000, 1_727_740_800),  # ≈ 2024-10-01
]


def estimate_registration_ts(user_id: int | None) -> int | None:
    """Оценить unix-время регистрации по user_id, либо None если id некорректен."""
    if not user_id:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    first_id, first_ts = _ANCHORS[0]
    last_id, last_ts = _ANCHORS[-1]
    if uid <= first_id:
        return first_ts
    if uid >= last_id:
        # экстраполяция по наклону последнего сегмента
        (x0, y0), (x1, y1) = _ANCHORS[-2], _ANCHORS[-1]
        if x1 == x0:
            return last_ts
        return int(y1 + (uid - x1) * (y1 - y0) / (x1 - x0))
    for (x0, y0), (x1, y1) in zip(_ANCHORS, _ANCHORS[1:]):
        if x0 <= uid <= x1:
            if x1 == x0:
                return y0
            return int(y0 + (uid - x0) * (y1 - y0) / (x1 - x0))
    return None


def estimate_registration_date(user_id: int | None) -> str | None:
    """Оценка даты регистрации в формате ISO 'YYYY-MM-DD' (UTC), либо None."""
    ts = estimate_registration_ts(user_id)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except (OSError, ValueError, OverflowError):
        return None
