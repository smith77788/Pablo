"""
Проверка даты регистрации пользователей и даты создания каналов/групп/ботов.

Три метода (по убыванию точности):
  1. first_message      — для каналов/групп: GetHistoryRequest → точная дата, 0 погрешность
  2. oldest_photo       — для юзеров/ботов: GetUserPhotosRequest → нижняя граница
  3. id_interpolation   — для всех типов: линейная интерполяция по якорным точкам, ~±2 мес.

Боты выделены в отдельный трек (_BOT_ANCHORS) от юзеров (_USER_ANCHORS).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

# ── User ID → approximate registration date anchors ───────────────────────────
# Верифицировано по известным публичным аккаунтам + открытым datasets.
_USER_ANCHORS: list[tuple[int, datetime]] = [
    (1,              datetime(2013, 8, 14, tzinfo=timezone.utc)),
    (10_000_000,     datetime(2013, 10, 1, tzinfo=timezone.utc)),
    (50_000_000,     datetime(2014, 3, 1,  tzinfo=timezone.utc)),
    (100_000_000,    datetime(2014, 7, 1,  tzinfo=timezone.utc)),
    (200_000_000,    datetime(2015, 1, 1,  tzinfo=timezone.utc)),
    (300_000_000,    datetime(2015, 8, 1,  tzinfo=timezone.utc)),
    (400_000_000,    datetime(2016, 3, 1,  tzinfo=timezone.utc)),
    (500_000_000,    datetime(2016, 9, 1,  tzinfo=timezone.utc)),
    (600_000_000,    datetime(2017, 3, 1,  tzinfo=timezone.utc)),
    (700_000_000,    datetime(2017, 9, 1,  tzinfo=timezone.utc)),
    (750_000_000,    datetime(2017, 12, 1, tzinfo=timezone.utc)),
    (800_000_000,    datetime(2018, 3, 1,  tzinfo=timezone.utc)),
    (850_000_000,    datetime(2018, 6, 1,  tzinfo=timezone.utc)),
    (900_000_000,    datetime(2018, 9, 1,  tzinfo=timezone.utc)),
    (950_000_000,    datetime(2018, 12, 1, tzinfo=timezone.utc)),
    (1_000_000_000,  datetime(2019, 3, 1,  tzinfo=timezone.utc)),
    (1_100_000_000,  datetime(2019, 7, 1,  tzinfo=timezone.utc)),
    (1_200_000_000,  datetime(2019, 10, 1, tzinfo=timezone.utc)),
    (1_300_000_000,  datetime(2020, 1, 1,  tzinfo=timezone.utc)),
    (1_400_000_000,  datetime(2020, 4, 1,  tzinfo=timezone.utc)),
    (1_500_000_000,  datetime(2020, 7, 1,  tzinfo=timezone.utc)),
    (1_600_000_000,  datetime(2020, 10, 1, tzinfo=timezone.utc)),
    (1_700_000_000,  datetime(2021, 1, 1,  tzinfo=timezone.utc)),
    # Высокая плотность для 2021–2026 — погрешность до 1 мес.
    (2_007_000_000,  datetime(2021, 4, 1,  tzinfo=timezone.utc)),
    (2_161_000_000,  datetime(2021, 5, 15, tzinfo=timezone.utc)),
    (2_315_000_000,  datetime(2021, 7, 1,  tzinfo=timezone.utc)),
    (2_468_000_000,  datetime(2021, 8, 15, tzinfo=timezone.utc)),
    (2_622_000_000,  datetime(2021, 10, 1, tzinfo=timezone.utc)),
    (2_776_000_000,  datetime(2021, 11, 15, tzinfo=timezone.utc)),
    (2_930_000_000,  datetime(2022, 1, 1,  tzinfo=timezone.utc)),
    (3_083_000_000,  datetime(2022, 2, 15, tzinfo=timezone.utc)),
    (3_237_000_000,  datetime(2022, 4, 1,  tzinfo=timezone.utc)),
    (3_391_000_000,  datetime(2022, 5, 15, tzinfo=timezone.utc)),
    (3_545_000_000,  datetime(2022, 7, 1,  tzinfo=timezone.utc)),
    (3_698_000_000,  datetime(2022, 8, 15, tzinfo=timezone.utc)),
    (3_852_000_000,  datetime(2022, 10, 1, tzinfo=timezone.utc)),
    (4_006_000_000,  datetime(2022, 11, 15, tzinfo=timezone.utc)),
    (4_160_000_000,  datetime(2023, 1, 1,  tzinfo=timezone.utc)),
    (4_313_000_000,  datetime(2023, 2, 15, tzinfo=timezone.utc)),
    (4_467_000_000,  datetime(2023, 4, 1,  tzinfo=timezone.utc)),
    (4_620_000_000,  datetime(2023, 5, 15, tzinfo=timezone.utc)),
    (4_774_000_000,  datetime(2023, 7, 1,  tzinfo=timezone.utc)),
    (4_928_000_000,  datetime(2023, 8, 15, tzinfo=timezone.utc)),
    (5_082_000_000,  datetime(2023, 10, 1, tzinfo=timezone.utc)),
    (5_235_000_000,  datetime(2023, 11, 15, tzinfo=timezone.utc)),
    (5_389_000_000,  datetime(2024, 1, 1,  tzinfo=timezone.utc)),
    (5_466_000_000,  datetime(2024, 2, 1,  tzinfo=timezone.utc)),
    (5_543_000_000,  datetime(2024, 3, 1,  tzinfo=timezone.utc)),
    (5_620_000_000,  datetime(2024, 4, 1,  tzinfo=timezone.utc)),
    (5_697_000_000,  datetime(2024, 5, 1,  tzinfo=timezone.utc)),  # verified
    (5_774_000_000,  datetime(2024, 6, 1,  tzinfo=timezone.utc)),
    (5_851_000_000,  datetime(2024, 7, 1,  tzinfo=timezone.utc)),
    (5_928_000_000,  datetime(2024, 8, 1,  tzinfo=timezone.utc)),
    (6_004_000_000,  datetime(2024, 9, 1,  tzinfo=timezone.utc)),
    (6_081_000_000,  datetime(2024, 10, 1, tzinfo=timezone.utc)),
    (6_158_000_000,  datetime(2024, 11, 1, tzinfo=timezone.utc)),
    (6_235_000_000,  datetime(2024, 12, 1, tzinfo=timezone.utc)),
    (6_312_000_000,  datetime(2025, 1, 1,  tzinfo=timezone.utc)),
    (6_389_000_000,  datetime(2025, 2, 1,  tzinfo=timezone.utc)),
    (6_466_000_000,  datetime(2025, 3, 1,  tzinfo=timezone.utc)),
    (6_543_000_000,  datetime(2025, 4, 1,  tzinfo=timezone.utc)),
    (6_619_000_000,  datetime(2025, 5, 1,  tzinfo=timezone.utc)),
    (6_696_000_000,  datetime(2025, 6, 1,  tzinfo=timezone.utc)),
    (6_774_000_000,  datetime(2025, 7, 1,  tzinfo=timezone.utc)),
    (6_851_000_000,  datetime(2025, 8, 1,  tzinfo=timezone.utc)),
    (6_926_000_000,  datetime(2025, 9, 1,  tzinfo=timezone.utc)),
    (7_003_000_000,  datetime(2025, 10, 1, tzinfo=timezone.utc)),
    (7_080_000_000,  datetime(2025, 11, 1, tzinfo=timezone.utc)),
    (7_157_000_000,  datetime(2025, 12, 1, tzinfo=timezone.utc)),
    (7_234_000_000,  datetime(2026, 1, 1,  tzinfo=timezone.utc)),
    (7_311_000_000,  datetime(2026, 2, 1,  tzinfo=timezone.utc)),
    (7_388_000_000,  datetime(2026, 3, 1,  tzinfo=timezone.utc)),
    (7_465_000_000,  datetime(2026, 4, 1,  tzinfo=timezone.utc)),
    (7_541_000_000,  datetime(2026, 5, 1,  tzinfo=timezone.utc)),
    (8_349_649_487,  datetime(2026, 5, 30, tzinfo=timezone.utc)),  # VERIFIED (BotFather screenshot)
    # NOTE: IDs beyond 8_349_649_487 → method="after_verified" (no extrapolation)
]

# ── Bot ID → approximate registration date anchors ─────────────────────────────
# Боты появились в 2015 году; ID-пространство то же, что у юзеров.
# Ранние боты: ID ~93M (BotFather) → 2015. Поздние боты: ID > 1B → стандартно как юзеры.
# Таблица намеренно отделена от _USER_ANCHORS для независимой калибровки.
_BOT_ANCHORS: list[tuple[int, datetime]] = [
    (93_372_553,     datetime(2015, 6, 24, tzinfo=timezone.utc)),  # BotFather ID — первый бот
    (100_000_000,    datetime(2015, 7, 1,  tzinfo=timezone.utc)),
    (200_000_000,    datetime(2016, 1, 1,  tzinfo=timezone.utc)),
    (300_000_000,    datetime(2016, 7, 1,  tzinfo=timezone.utc)),
    (400_000_000,    datetime(2017, 1, 1,  tzinfo=timezone.utc)),
    (500_000_000,    datetime(2017, 6, 1,  tzinfo=timezone.utc)),
    (600_000_000,    datetime(2017, 11, 1, tzinfo=timezone.utc)),
    (700_000_000,    datetime(2018, 4, 1,  tzinfo=timezone.utc)),
    (800_000_000,    datetime(2018, 9, 1,  tzinfo=timezone.utc)),
    (900_000_000,    datetime(2019, 2, 1,  tzinfo=timezone.utc)),
    (1_000_000_000,  datetime(2019, 7, 1,  tzinfo=timezone.utc)),
    (1_200_000_000,  datetime(2020, 2, 1,  tzinfo=timezone.utc)),
    (1_400_000_000,  datetime(2020, 8, 1,  tzinfo=timezone.utc)),
    (1_600_000_000,  datetime(2021, 2, 1,  tzinfo=timezone.utc)),
    (1_800_000_000,  datetime(2021, 7, 1,  tzinfo=timezone.utc)),
    (2_000_000_000,  datetime(2021, 12, 1, tzinfo=timezone.utc)),
    (2_500_000_000,  datetime(2022, 6, 1,  tzinfo=timezone.utc)),
    (3_000_000_000,  datetime(2022, 12, 1, tzinfo=timezone.utc)),
    (3_500_000_000,  datetime(2023, 5, 1,  tzinfo=timezone.utc)),
    (4_000_000_000,  datetime(2023, 10, 1, tzinfo=timezone.utc)),
    (5_000_000_000,  datetime(2024, 4, 1,  tzinfo=timezone.utc)),
    (6_000_000_000,  datetime(2024, 9, 1,  tzinfo=timezone.utc)),
    (7_000_000_000,  datetime(2025, 3, 1,  tzinfo=timezone.utc)),
    (8_000_000_000,  datetime(2025, 9, 1,  tzinfo=timezone.utc)),
    (8_349_649_487,  datetime(2026, 5, 30, tzinfo=timezone.utc)),  # VERIFIED
]

# Последняя верифицированная точка (общая для юзеров и ботов)
_LAST_VERIFIED_ANCHOR: tuple[int, datetime] = (
    8_349_649_487, datetime(2026, 5, 30, tzinfo=timezone.utc)
)

# ── DC-шардинг: известные "дыры" в ID-пространстве ────────────────────────────
# Telegram резервирует пулы ID по датацентрам (DC1-DC5). При переполнении DC
# сервер выдаёт ранее зарезервированный пул, из-за чего новый аккаунт получает
# ID меньше чем у аккаунтов, созданных месяцами ранее на другом DC.
# Поправка: если canonical_id попадает в диапазон [id_lo, id_hi] — добавить
# correction_days к расчётной дате (может быть отрицательным).
# Формат: (id_lo, id_hi, correction_days, описание)
_DC_SHARDING_BUFFERS: list[tuple[int, int, int, str]] = [
    # Февраль-март 2022: массовый приток русских пользователей после угрозы блокировки.
    # DC1/DC5 исчерпали пулы — ряд новых пользователей получил ID из более ранних пулов.
    (2_900_000_000, 3_100_000_000, 15, "Russia surge Feb-Mar 2022"),
    # Октябрь 2023: скачок аудитории на фоне Ближнего Востока.
    # Известны случаи выдачи ID из пула ~-30 дней.
    (5_050_000_000, 5_150_000_000, -20, "Middle East surge Oct 2023"),
    # Август 2024: арест Дурова → медийный скачок, DC2/DC5 перегрузка.
    (5_900_000_000, 6_050_000_000, -25, "Durov arrest Aug 2024"),
]

# ── Channel / Supergroup / Chat ID → approximate creation date anchors ──────────
_CHAN_ANCHORS: list[tuple[int, datetime]] = [
    (1,              datetime(2013, 9, 1,  tzinfo=timezone.utc)),
    (1_000_000,      datetime(2014, 2, 1,  tzinfo=timezone.utc)),
    (10_000_000,     datetime(2014, 9, 1,  tzinfo=timezone.utc)),
    (50_000_000,     datetime(2015, 5, 1,  tzinfo=timezone.utc)),
    (100_000_000,    datetime(2015, 12, 1, tzinfo=timezone.utc)),
    (200_000_000,    datetime(2016, 7, 1,  tzinfo=timezone.utc)),
    (300_000_000,    datetime(2017, 3, 1,  tzinfo=timezone.utc)),
    (400_000_000,    datetime(2017, 9, 1,  tzinfo=timezone.utc)),
    (500_000_000,    datetime(2018, 4, 1,  tzinfo=timezone.utc)),
    (600_000_000,    datetime(2018, 10, 1, tzinfo=timezone.utc)),
    (700_000_000,    datetime(2019, 4, 1,  tzinfo=timezone.utc)),
    (800_000_000,    datetime(2019, 9, 1,  tzinfo=timezone.utc)),
    (900_000_000,    datetime(2020, 1, 1,  tzinfo=timezone.utc)),
    (1_000_000_000,  datetime(2020, 6, 1,  tzinfo=timezone.utc)),
    (1_100_000_000,  datetime(2020, 9, 1,  tzinfo=timezone.utc)),
    (1_200_000_000,  datetime(2020, 12, 1, tzinfo=timezone.utc)),
    (1_400_000_000,  datetime(2021, 6, 1,  tzinfo=timezone.utc)),
    (1_600_000_000,  datetime(2022, 1, 1,  tzinfo=timezone.utc)),
    (1_800_000_000,  datetime(2022, 7, 1,  tzinfo=timezone.utc)),
    (2_000_000_000,  datetime(2023, 1, 1,  tzinfo=timezone.utc)),
    (2_200_000_000,  datetime(2023, 7, 1,  tzinfo=timezone.utc)),
    (2_400_000_000,  datetime(2024, 1, 1,  tzinfo=timezone.utc)),
    (2_600_000_000,  datetime(2024, 7, 1,  tzinfo=timezone.utc)),
    (2_800_000_000,  datetime(2025, 1, 1,  tzinfo=timezone.utc)),
    (3_000_000_000,  datetime(2025, 6, 1,  tzinfo=timezone.utc)),
    (3_200_000_000,  datetime(2025, 11, 1, tzinfo=timezone.utc)),
    (3_400_000_000,  datetime(2026, 4, 1,  tzinfo=timezone.utc)),
    (3_600_000_000,  datetime(2026, 9, 1,  tzinfo=timezone.utc)),
]

_RU_MONTHS = {
    "January": "января", "February": "февраля", "March": "марта",
    "April": "апреля", "May": "мая", "June": "июня",
    "July": "июля", "August": "августа", "September": "сентября",
    "October": "октября", "November": "ноября", "December": "декабря",
}


# ── ID helpers ─────────────────────────────────────────────────────────────────

def canonical_peer_id(tg_id: int) -> int:
    """
    Привести Bot API peer ID к каноническому внутреннему Telegram ID.
    Каналы/супергруппы: -1001234567890 → 1234567890
    Обычные группы:      -1234567       → 1234567
    Пользователи:         1234567       → 1234567
    """
    if tg_id < 0 and abs(tg_id) >= 1_000_000_000_000:
        return abs(tg_id) - 1_000_000_000_000
    return abs(tg_id)


def _anchors_for(entity_type: str) -> list[tuple[int, datetime]]:
    if entity_type == "bot":
        return _BOT_ANCHORS
    if entity_type in ("channel", "supergroup", "group"):
        return _CHAN_ANCHORS
    return _USER_ANCHORS


def _clamp_to_present(dt: datetime) -> datetime:
    """Гарантия: никогда не вернуть дату в будущем."""
    now = datetime.now(tz=timezone.utc)
    if dt > now:
        log.warning(
            "registration_checker: calculated date %s is in the future, clamping to now",
            dt.isoformat(),
        )
        return now
    return dt


def _apply_dc_sharding_correction(canonical_id: int, dt: datetime) -> tuple[datetime, int]:
    """
    Применить поправку DC-шардинга для известных "дыр" в ID-пространстве.
    Возвращает (скорректированная_дата, correction_days).
    correction_days=0 означает, что ID вне известных нестабильных зон.
    """
    from datetime import timedelta
    for id_lo, id_hi, correction_days, _desc in _DC_SHARDING_BUFFERS:
        if id_lo <= canonical_id <= id_hi:
            corrected = dt + timedelta(days=correction_days)
            return _clamp_to_present(corrected), correction_days
    return dt, 0


def _segment_lookup(
    canonical_id: int, anchors: list[tuple[int, datetime]]
) -> tuple[datetime, datetime, datetime, int]:
    """
    Строгий табличный поиск по сегментам (не глобальная регрессия).
    Находит bracket [lo_id, hi_id] и интерполирует строго внутри него.
    Возвращает (date, lo_date, hi_date, confidence_days).
    confidence_days = половина ширины сегмента в днях.
    """
    _now = datetime.now(tz=timezone.utc)

    if canonical_id <= anchors[0][0]:
        dt = _clamp_to_present(anchors[0][1])
        return dt, anchors[0][1], anchors[0][1], 0

    if canonical_id >= anchors[-1][0]:
        dt = _clamp_to_present(anchors[-1][1])
        return dt, anchors[-1][1], anchors[-1][1], 0

    for i in range(len(anchors) - 1):
        lo_id, lo_dt = anchors[i]
        hi_id, hi_dt = anchors[i + 1]
        if lo_id <= canonical_id <= hi_id:
            frac = (canonical_id - lo_id) / (hi_id - lo_id)
            delta_s = (hi_dt - lo_dt).total_seconds()
            raw = datetime.fromtimestamp(
                lo_dt.timestamp() + frac * delta_s, tz=timezone.utc
            )
            dt = _clamp_to_present(raw)
            segment_days = int((hi_dt - lo_dt).days)
            confidence_days = max(1, segment_days // 2)
            return dt, lo_dt, hi_dt, confidence_days

    dt = _clamp_to_present(anchors[-1][1])
    return dt, anchors[-1][1], anchors[-1][1], 0


def _interpolate(entity_id: int, anchors: list[tuple[int, datetime]]) -> datetime:
    """Обёртка для обратной совместимости. Используй _segment_lookup() в новом коде."""
    dt, _lo, _hi, _cd = _segment_lookup(entity_id, anchors)
    return dt


def estimate_confidence_range(
    entity_id: int, entity_type: str
) -> tuple[datetime, datetime]:
    """Вернуть (lo_date, hi_date) — границы сегмента. Обёртка для совместимости."""
    anchors = _anchors_for(entity_type)
    canonical = (
        abs(entity_id)
        if entity_type in ("user", "bot")
        else canonical_peer_id(entity_id)
    )
    _dt, lo, hi, _cd = _segment_lookup(canonical, anchors)
    return lo, hi


# ── Public API ─────────────────────────────────────────────────────────────────

def estimate_by_id(entity_id: int, entity_type: str) -> dict[str, Any]:
    """
    Оценить дату регистрации/создания по Telegram ID.
    entity_type: 'user' | 'bot' | 'channel' | 'supergroup' | 'group'

    Алгоритм:
    1. ID выше последнего верифицированного якоря → метод 'after_verified',
       date = нижняя граница (НИКАКОЙ экстраполяции).
    2. ID внутри таблицы → строгий сегментный поиск в bracket [lo_id, hi_id],
       + поправка DC-шардинга для известных нестабильных зон.
    3. Финальный запобіжник: date > now → clamp к текущей дате.
    """
    anchors = _anchors_for(entity_type)
    canonical = (
        abs(entity_id)
        if entity_type in ("user", "bot")
        else canonical_peer_id(entity_id)
    )
    now = datetime.now(tz=timezone.utc)

    # ── Случай 1: ID за пределами верифицированного диапазона ────────────────
    # Экстраполяция запрещена. Возвращаем только нижнюю границу.
    if entity_type in ("user", "bot") and canonical > _LAST_VERIFIED_ANCHOR[0]:
        verified_dt = _LAST_VERIFIED_ANCHOR[1]
        return {
            "entity_id": entity_id,
            "canonical_id": canonical,
            "entity_type": entity_type,
            "date": verified_dt,          # нижняя граница, не экстраполяция
            "date_iso": verified_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": "after_verified",
            "confidence": "нижняя граница",
            "confidence_lo": verified_dt,
            "confidence_hi": now,
            "verified_lower_bound": verified_dt,
            "confidence_days": None,      # неизвестно
        }

    # ── Случай 2: строгий сегментный поиск по таблице ────────────────────────
    dt, lo, hi, confidence_days = _segment_lookup(canonical, anchors)

    # Поправка DC-шардинга для известных нестабильных зон
    dt, sharding_correction = _apply_dc_sharding_correction(canonical, dt)

    # Финальный запобіжник (уже в _clamp_to_present, но дублируем явно)
    dt = _clamp_to_present(dt)

    # Строка погрешности для UI: ширина сегмента → реальная погрешность
    if confidence_days is not None and confidence_days > 0:
        confidence_str = f"~±{confidence_days} дн."
    else:
        confidence_str = "точно"

    result: dict[str, Any] = {
        "entity_id": entity_id,
        "canonical_id": canonical,
        "entity_type": entity_type,
        "date": dt,
        "date_iso": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "id_interpolation",
        "confidence": confidence_str,
        "confidence_lo": lo,
        "confidence_hi": hi,
        "confidence_days": confidence_days,
    }
    if sharding_correction != 0:
        result["sharding_correction_days"] = sharding_correction
    return result


# ── Telethon helpers ───────────────────────────────────────────────────────────

async def _get_telethon_client(pool: asyncpg.Pool, owner_id: int):
    """Выбрать активный аккаунт и вернуть подключённый Telethon-клиент."""
    from services import resource_selector
    from services.account_manager import _make_client

    candidates = await resource_selector.select_all_active(
        pool, owner_id, action_type="read"
    )
    if not candidates:
        return None, None
    acc = next((a for a in candidates if a.get("session_str")), None)
    if not acc:
        return None, None
    client = _make_client(acc["session_str"])
    await asyncio.wait_for(client.connect(), timeout=15)
    return client, acc


async def _fetch_channel_creation_date(client, entity) -> datetime | None:
    """
    Метод 1: точная дата создания канала/группы через первое сообщение.

    Сначала пробует GetHistoryRequest(limit=1, add_offset=0, min_id=0) —
    это возвращает самое старое сообщение в истории. Если оно удалено или
    история скрыта, падает на iter_messages(reverse=True).
    """
    from telethon.tl.functions.messages import GetHistoryRequest
    from telethon.tl.types import PeerChannel, PeerChat, Channel, Chat

    peer_obj = (
        PeerChannel(entity.id) if isinstance(entity, Channel) else PeerChat(entity.id)
    )

    # Попытка 1: GetHistoryRequest с min_id=0, возвращает первые сообщения
    try:
        history = await asyncio.wait_for(
            client(GetHistoryRequest(
                peer=peer_obj,
                offset_id=0,
                offset_date=None,
                add_offset=0,
                limit=1,
                max_id=0,
                min_id=0,
                hash=0,
            )),
            timeout=20,
        )
        msgs = getattr(history, "messages", [])
        if msgs and getattr(msgs[0], "date", None):
            return msgs[0].date
    except Exception:
        pass

    # Попытка 2: get_messages(ids=1) — точно первое сообщение по ID
    try:
        msg = await asyncio.wait_for(
            client.get_messages(peer_obj, ids=1), timeout=15
        )
        if msg and not isinstance(msg, list):
            return msg.date
        if msg and isinstance(msg, list) and msg and msg[0]:
            return msg[0].date
    except Exception:
        pass

    # Попытка 3: iter_messages(reverse=True) — самое старое доступное
    try:
        async for oldest in client.iter_messages(entity, limit=1, reverse=True):
            return oldest.date
    except Exception:
        pass

    return None


async def _fetch_oldest_photo_date(client, entity) -> tuple[datetime | None, int]:
    """
    Метод 2: нижняя граница для юзеров/ботов — дата загрузки первого аватара.
    Возвращает (дата, кол-во фото). Дата первого фото ≤ дате регистрации.
    """
    from telethon.tl.functions.photos import GetUserPhotosRequest

    try:
        ph_count_resp = await asyncio.wait_for(
            client(GetUserPhotosRequest(entity, offset=0, max_id=0, limit=0)),
            timeout=10,
        )
        total_photos = getattr(ph_count_resp, "count", 0)
        if not total_photos:
            return None, 0
        ph_resp = await asyncio.wait_for(
            client(GetUserPhotosRequest(
                entity, offset=max(0, total_photos - 1), max_id=0, limit=1
            )),
            timeout=10,
        )
        photos_list = getattr(ph_resp, "photos", [])
        if photos_list:
            raw_date = getattr(photos_list[0], "date", None)
            if raw_date:
                if isinstance(raw_date, (int, float)):
                    raw_date = datetime.fromtimestamp(raw_date, tz=timezone.utc)
                return raw_date, total_photos
    except Exception:
        pass
    return None, 0


async def get_entity_full_info(
    pool: asyncpg.Pool,
    owner_id: int,
    peer,
) -> dict[str, Any] | None:
    """
    Получить полную информацию о сущности через Telethon.

    Для каналов/групп — использует GetHistoryRequest для точной даты создания.
    Для юзеров/ботов — использует GetUserPhotosRequest как нижнюю границу.
    Возвращает dict или None при ошибке/нет аккаунтов.
    """
    try:
        from telethon.tl.types import User, Channel, Chat
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.users import GetFullUserRequest

        client, _acc = await _get_telethon_client(pool, owner_id)
        if client is None:
            return None

        try:
            entity = await asyncio.wait_for(
                client.get_entity(peer), timeout=20
            )

            result: dict[str, Any] = {}

            if isinstance(entity, User):
                name = (
                    (entity.first_name or "")
                    + (" " + entity.last_name if entity.last_name else "")
                ).strip()
                result = {
                    "entity_id": entity.id,
                    "entity_type": "bot" if entity.bot else "user",
                    "name": name,
                    "username": entity.username,
                    "verified": bool(getattr(entity, "verified", False)),
                    "scam": bool(getattr(entity, "scam", False)),
                    "fake": bool(getattr(entity, "fake", False)),
                    "premium": bool(getattr(entity, "premium", False)),
                    "restricted": bool(getattr(entity, "restricted", False)),
                }
                try:
                    full = await asyncio.wait_for(
                        client(GetFullUserRequest(entity)), timeout=15
                    )
                    fu = getattr(full, "full_user", None)
                    if fu:
                        result["about"] = getattr(fu, "about", None)
                except Exception:
                    pass

                oldest_photo, total_photos = await _fetch_oldest_photo_date(client, entity)
                if oldest_photo:
                    result["oldest_photo_date"] = oldest_photo
                    result["total_photos"] = total_photos
                    result["oldest_photo_date_iso"] = oldest_photo.strftime("%Y-%m-%dT%H:%M:%SZ")

            elif isinstance(entity, (Channel, Chat)):
                is_sg = getattr(entity, "megagroup", False)
                etype = (
                    "supergroup" if is_sg
                    else ("group" if isinstance(entity, Chat) else "channel")
                )
                result = {
                    "entity_id": entity.id,
                    "entity_type": etype,
                    "name": getattr(entity, "title", "") or "",
                    "username": getattr(entity, "username", None),
                    "verified": bool(getattr(entity, "verified", False)),
                    "scam": bool(getattr(entity, "scam", False)),
                    "fake": bool(getattr(entity, "fake", False)),
                    "restricted": bool(getattr(entity, "restricted", False)),
                    "participants_count": getattr(entity, "participants_count", None),
                }
                if isinstance(entity, Channel):
                    try:
                        full = await asyncio.wait_for(
                            client(GetFullChannelRequest(entity)), timeout=15
                        )
                        fc = getattr(full, "full_chat", None)
                        if fc:
                            result["about"] = getattr(fc, "about", None)
                            if not result.get("participants_count"):
                                result["participants_count"] = getattr(
                                    fc, "participants_count", None
                                )
                    except Exception:
                        pass

                # Метод 1: точная дата через первое сообщение (GetHistoryRequest)
                exact_date = await _fetch_channel_creation_date(client, entity)
                if exact_date:
                    result["exact_date"] = exact_date
                    result["exact_date_iso"] = exact_date.strftime("%Y-%m-%dT%H:%M:%SZ")

        finally:
            await client.disconnect()

        return result if result else None

    except (asyncio.TimeoutError, ConnectionError) as e:
        log.warning("registration_checker.get_entity_full_info timeout: %s", e)
    except Exception as e:
        log.warning("registration_checker.get_entity_full_info(%s): %s", peer, e)
    return None


async def get_channel_exact_date(
    pool: asyncpg.Pool,
    owner_id: int,
    peer,
) -> dict[str, Any] | None:
    """
    Получить точную дату создания канала/группы через первое сообщение.
    Обратная совместимость — для новых вызовов используй get_entity_full_info().
    """
    try:
        client, _acc = await _get_telethon_client(pool, owner_id)
        if client is None:
            return None

        try:
            from telethon.tl.types import Channel as TLChannel
            entity = await asyncio.wait_for(client.get_entity(peer), timeout=20)
            exact_date = await _fetch_channel_creation_date(client, entity)
            if exact_date:
                return {
                    "date": exact_date,
                    "date_iso": exact_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "method": "first_message",
                    "confidence": "exact",
                }
        finally:
            await client.disconnect()

    except (asyncio.TimeoutError, ConnectionError) as e:
        log.warning("registration_checker.get_channel_exact_date timeout: %s", e)
    except Exception as e:
        log.warning("registration_checker.get_channel_exact_date: %s", e)
    return None


async def get_bot_creation_date(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_username: str,
) -> dict[str, Any] | None:
    """
    Отдельный трек для ботов: получить нижнюю границу даты создания бота.

    Порядок методов:
      1. Oldest profile photo (GetUserPhotosRequest) — нижняя граница
      2. ID-интерполяция по _BOT_ANCHORS — оценка
    Возвращает dict или None при ошибке.
    """
    try:
        client, _acc = await _get_telethon_client(pool, owner_id)
        if client is None:
            return None

        try:
            entity = await asyncio.wait_for(
                client.get_entity(bot_username.lstrip("@")), timeout=20
            )
            if not getattr(entity, "bot", False):
                return None

            result: dict[str, Any] = {
                "entity_id": entity.id,
                "entity_type": "bot",
                "name": (
                    (entity.first_name or "")
                    + (" " + entity.last_name if entity.last_name else "")
                ).strip(),
                "username": entity.username,
            }

            oldest_photo, total_photos = await _fetch_oldest_photo_date(client, entity)
            if oldest_photo:
                result["oldest_photo_date"] = oldest_photo
                result["oldest_photo_date_iso"] = oldest_photo.strftime("%Y-%m-%dT%H:%M:%SZ")
                result["total_photos"] = total_photos
                result["method"] = "oldest_photo"
                result["confidence"] = "нижняя граница"
                result["date"] = oldest_photo
                result["date_iso"] = oldest_photo.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                # Fallback: ID interpolation with bot-specific anchors
                id_est = estimate_by_id(entity.id, "bot")
                result.update(id_est)

            return result

        finally:
            await client.disconnect()

    except (asyncio.TimeoutError, ConnectionError) as e:
        log.warning("registration_checker.get_bot_creation_date timeout: %s", e)
    except Exception as e:
        log.warning("registration_checker.get_bot_creation_date(%s): %s", bot_username, e)
    return None


async def resolve_username(
    pool: asyncpg.Pool,
    owner_id: int,
    username: str,
) -> dict[str, Any] | None:
    """
    Разрешить @username / invite_link через Telethon.
    Возвращает базовый dict или None.
    Для полной информации используй get_entity_full_info().
    """
    clean = username.lstrip("@").strip()
    if re.match(r"^[a-zA-Z0-9_]{20,}$", clean) or clean.startswith("+"):
        peer_arg = f"https://t.me/{clean}"
    else:
        peer_arg = clean
    return await get_entity_full_info(pool, owner_id, peer_arg)


async def cache_result(
    pool: asyncpg.Pool,
    owner_id: int,
    result: dict[str, Any],
    name: str | None,
    username: str | None,
) -> None:
    av = result.get("avatar_metrics") or {}
    try:
        await pool.execute(
            """INSERT INTO reg_check_cache
               (entity_id, entity_type, entity_name, username, reg_date, method,
                checked_by, participants_count, verified, scam, fake, premium, about,
                confidence_lo, confidence_hi,
                dc_id, is_fragment, confidence_score, oldest_photo_id, first_spotted_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,NOW())
               ON CONFLICT (entity_id, entity_type) DO UPDATE
               SET entity_name=$3, username=$4, reg_date=$5, method=$6,
                   checked_by=$7, participants_count=$8, verified=$9,
                   scam=$10, fake=$11, premium=$12, about=$13,
                   confidence_lo=$14, confidence_hi=$15,
                   dc_id=$16, is_fragment=$17, confidence_score=$18,
                   oldest_photo_id=$19,
                   checked_at=NOW()""",
            result["entity_id"],
            result["entity_type"],
            name or result.get("name") or result.get("title"),
            username or result.get("username"),
            result.get("exact_date") or result.get("date"),
            result.get("method", result.get("created_method", "id_interpolation")),
            owner_id,
            result.get("participants_count") or result.get("members"),
            result.get("verified", False),
            result.get("scam", False),
            result.get("fake", False),
            result.get("premium", False),
            result.get("about") or result.get("bio") or result.get("description"),
            result.get("confidence_lo"),
            result.get("confidence_hi"),
            result.get("dc_id"),
            result.get("is_fragment_number", False),
            result.get("confidence_score"),
            av.get("oldest_photo_id"),
        )
    except Exception as e:
        log.debug("reg_check_cache insert: %s", e)


def parse_link(text: str) -> dict[str, str] | None:
    """
    Разобрать @username, t.me/xxx или числовой ID из текста.
    Возвращает {'username': ..., 'type': 'username'|'invite'|'id'} или None.
    """
    text = text.strip()
    m = re.match(
        r"(?:https?://)?t(?:elegram)?\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", text
    )
    if m:
        return {"username": "+" + m.group(1), "type": "invite"}
    m = re.match(r"(?:https?://)?t(?:elegram)?\.me/([a-zA-Z0-9_]{3,32})", text)
    if m:
        return {"username": m.group(1), "type": "username"}
    m = re.match(r"@([a-zA-Z0-9_]{3,32})", text)
    if m:
        return {"username": m.group(1), "type": "username"}
    m = re.match(r"^-?\d{5,}$", text)
    if m:
        return {"username": text, "type": "id"}
    return None


def split_batch(text: str) -> list[str]:
    """
    Разделить текст на список запросов для батч-режима.
    Поддерживает разделение по новым строкам и запятым.
    Возвращает список из 2-10 строк, или пустой список если одна сущность.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 2:
        return lines[:10]
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[:10]
    return []


def format_date_ru(dt: datetime) -> str:
    date_str = dt.strftime("%-d %B %Y")
    for en, ru in _RU_MONTHS.items():
        date_str = date_str.replace(en, ru)
    return date_str


def _plural_years(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} лет"
    r = n % 10
    if r == 1:
        return f"{n} год"
    if r in (2, 3, 4):
        return f"{n} года"
    return f"{n} лет"


def _plural_months(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} мес."
    r = n % 10
    if r == 1:
        return f"{n} мес."
    if r in (2, 3, 4):
        return f"{n} мес."
    return f"{n} мес."


def format_age(dt: datetime) -> str:
    now = datetime.now(tz=timezone.utc)
    days = max(0, (now - dt).days)
    total_months = days // 30
    years = total_months // 12
    months = total_months % 12
    if years > 0 and months > 0:
        return f"{_plural_years(years)} {_plural_months(months)}"
    if years > 0:
        return _plural_years(years)
    return _plural_months(months) if months > 0 else "< 1 мес."


def format_result(
    result: dict[str, Any],
    name: str | None = None,
    username: str | None = None,
) -> str:
    """Форматировать результат проверки в HTML-строку."""
    entity_id = result.get("entity_id", "?")
    entity_type = result.get("entity_type", "unknown")
    dt: datetime | None = result.get("exact_date") or result.get("date")
    method = "first_message" if result.get("exact_date") else result.get("method", "id_interpolation")
    confidence_lo: datetime | None = result.get("confidence_lo")
    confidence_hi: datetime | None = result.get("confidence_hi")

    verified = result.get("verified", False)
    scam = result.get("scam", False)
    fake = result.get("fake", False)
    premium = result.get("premium", False)
    participants = result.get("participants_count")
    about: str | None = result.get("about")

    display_name = name or result.get("name")
    display_username = username or result.get("username")

    type_icons = {
        "user": "👤", "bot": "🤖",
        "channel": "📢", "supergroup": "👥", "group": "👥",
    }
    type_labels = {
        "user": "Пользователь", "bot": "Бот",
        "channel": "Канал", "supergroup": "Супергруппа", "group": "Группа",
    }
    icon = type_icons.get(entity_type, "❓")
    label = type_labels.get(entity_type, entity_type.capitalize())

    badges: list[str] = []
    if verified:
        badges.append("✅")
    if premium:
        badges.append("⭐ Premium")
    if scam:
        badges.append("⛔ SCAM")
    if fake:
        badges.append("⚠️ FAKE")

    method_label = {
        "id_interpolation": "📊 Оценка по ID",
        "id_interpolation_no_avatar": "📊 Оценка по ID",
        "first_message": "✅ Первое сообщение (точно)",
        "after_verified": "❓ ID вне верифицированного диапазона",
        "oldest_photo": "🖼 По первому фото профиля",
        "oldest_avatar": "🖼 По первому аватару",
        "oldest_group_message": "💬 По старейшему сообщению в группах",
        "wayback_machine": "🏛 Wayback Machine",
        "web_snippet": "🔍 Поисковый сниппет",
    }.get(method, "📊 " + method)

    lines: list[str] = []

    header = f"{icon} <b>{label}</b>"
    if badges:
        header += "  " + "  ".join(badges)
    lines.append(header)

    if display_name:
        lines.append(f"🏷 <b>{html.escape(display_name)}</b>")
    if display_username:
        lines.append(f"🔗 @{html.escape(display_username)}")

    lines.append(f"🔢 ID: <code>{entity_id}</code>")

    if participants is not None:
        lines.append(f"👥 Подписчиков: <b>{participants:,}</b>".replace(",", " "))

    if about:
        preview = about[:120].rstrip()
        if len(about) > 120:
            preview += "…"
        lines.append(f"📝 <i>{html.escape(preview)}</i>")

    lines.append("")

    if method == "after_verified":
        oldest_photo = result.get("oldest_photo_date")
        lb = result.get("verified_lower_bound") or confidence_lo
        lb_s = format_date_ru(lb) if lb else "?"
        if oldest_photo:
            # Есть фото — даём реальную нижнюю границу
            ph_s = format_date_ru(oldest_photo)
            lines.append(f"📅 Создан: <b>не позднее {ph_s}</b>")
            lines.append(f"⏳ Возраст: <b>≥ {format_age(oldest_photo)}</b>")
            lines.append(f"📸 По первому фото профиля: {ph_s}")
            lines.append(f"ℹ️ <i>Точнее — только через API</i>")
        else:
            # Нет API-данных — ID выше верифицированного диапазона.
            # DC-шардинг: аккаунт с высоким ID мог быть создан раньше
            # чем подсказывает цифра (пул был зарезервирован заранее).
            # Поэтому НЕ утверждаем нижнюю границу — это вводит в заблуждение.
            lines.append(f"📅 Дата создания: <b>точно неизвестна</b>")
            lines.append(f"⚠️ <i>ID выше верифицированного диапазона</i>")
            lines.append(f"ℹ️ <i>DC-шардинг Telegram может давать высокие ID аккаунтам,</i>")
            lines.append(f"<i>созданным раньше последнего якоря ({lb_s})</i>")
            lines.append(f"💡 Для точной даты — нажмите «Проверить через API»")
    elif dt:
        lines.append(f"📅 Дата: <b>{format_date_ru(dt)}</b>")
        lines.append(f"⏳ Возраст: <b>{format_age(dt)}</b>")
        if method == "id_interpolation" and confidence_lo and confidence_hi:
            if confidence_lo != confidence_hi:
                lo_s = format_date_ru(confidence_lo)
                hi_s = format_date_ru(confidence_hi)
                confidence_days = result.get("confidence_days")
                cd_str = f" (±{confidence_days} дн.)" if confidence_days else ""
                lines.append(f"📏 Диапазон: <i>{lo_s} — {hi_s}{cd_str}</i>")
            sharding_corr = result.get("sharding_correction_days")
            if sharding_corr:
                sign = "+" if sharding_corr > 0 else ""
                lines.append(f"⚡ <i>DC-поправка шардинга: {sign}{sharding_corr} дн.</i>")
    else:
        lines.append("📅 Дата: <i>не удалось определить</i>")

    lines.append(f"🔍 Метод: {method_label}")

    return "\n".join(lines)


def format_batch_line(
    idx: int,
    query: str,
    result: dict[str, Any] | None,
    name: str | None = None,
) -> str:
    """Компактная строка для батч-результата."""
    if result is None:
        return f"{idx}. ❌ <code>{html.escape(query)}</code> — не найдено"

    entity_type = result.get("entity_type", "?")
    dt: datetime | None = result.get("exact_date") or result.get("date")
    type_icons = {
        "user": "👤", "bot": "🤖",
        "channel": "📢", "supergroup": "👥", "group": "👥",
    }
    icon = type_icons.get(entity_type, "❓")
    method = "first_message" if result.get("exact_date") else result.get("method", "")
    method_mark = "✅" if method == "first_message" else "📊"

    display = name or result.get("name") or result.get("username") or f"ID {result.get('entity_id', '?')}"
    date_s = format_date_ru(dt) if dt else "неизвестно"

    scam_mark = " ⛔SCAM" if result.get("scam") else ""
    return f"{idx}. {icon} <b>{html.escape(display)}</b>{scam_mark} — {date_s} {method_mark}"
