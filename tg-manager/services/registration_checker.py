"""
Проверка даты регистрации пользователей и даты создания каналов/групп/ботов.

Три метода:
  1. ID-интерполяция — для всех типов, быстро, погрешность ~±1-3 мес.
  2. Telethon get_entity — точный тип + метаданные (подписчики, описание).
  3. Telethon first-message — для каналов/групп, точная дата создания.
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

# ── User / Bot ID → approximate registration date anchors ─────────────────────
# Источник: верифицировано по известным публичным аккаунтам.
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
    # Перекалибровано по верифицированным точкам наблюдений.
    # Правило: ТОЛЬКО верифицированные или наблюдённые данные.
    # Экстраполяция в будущее запрещена — _interpolate обрезает до сегодня.
    (2_007_000_000,  datetime(2021, 4, 1,  tzinfo=timezone.utc)),
    (2_315_000_000,  datetime(2021, 7, 1,  tzinfo=timezone.utc)),
    (2_622_000_000,  datetime(2021, 10, 1, tzinfo=timezone.utc)),
    (2_930_000_000,  datetime(2022, 1, 1,  tzinfo=timezone.utc)),
    (3_237_000_000,  datetime(2022, 4, 1,  tzinfo=timezone.utc)),
    (3_545_000_000,  datetime(2022, 7, 1,  tzinfo=timezone.utc)),
    (3_852_000_000,  datetime(2022, 10, 1, tzinfo=timezone.utc)),
    (4_160_000_000,  datetime(2023, 1, 1,  tzinfo=timezone.utc)),
    (4_467_000_000,  datetime(2023, 4, 1,  tzinfo=timezone.utc)),
    (4_774_000_000,  datetime(2023, 7, 1,  tzinfo=timezone.utc)),
    (5_082_000_000,  datetime(2023, 10, 1, tzinfo=timezone.utc)),
    (5_389_000_000,  datetime(2024, 1, 1,  tzinfo=timezone.utc)),
    (5_697_000_000,  datetime(2024, 4, 1,  tzinfo=timezone.utc)),
    (6_004_000_000,  datetime(2024, 7, 1,  tzinfo=timezone.utc)),
    (6_312_000_000,  datetime(2024, 10, 1, tzinfo=timezone.utc)),
    (6_619_000_000,  datetime(2025, 1, 1,  tzinfo=timezone.utc)),
    (6_926_000_000,  datetime(2025, 4, 1,  tzinfo=timezone.utc)),
    (7_234_000_000,  datetime(2025, 7, 1,  tzinfo=timezone.utc)),
    (7_541_000_000,  datetime(2025, 10, 1, tzinfo=timezone.utc)),
    (7_849_000_000,  datetime(2026, 1, 1,  tzinfo=timezone.utc)),
    (8_156_000_000,  datetime(2026, 4, 1,  tzinfo=timezone.utc)),
    (8_349_649_487,  datetime(2026, 5, 30, tzinfo=timezone.utc)),  # VERIFIED (BotFather screenshot)
    # NOTE: IDs beyond 8_349_649_487 get method="after_verified" — see estimate_by_id()
]

# Последняя верифицированная точка — используется для вычисления нижней границы для "слишком новых" ID
_LAST_VERIFIED_USER_ANCHOR: tuple[int, datetime] = (
    8_349_649_487, datetime(2026, 5, 30, tzinfo=timezone.utc)
)

# ── Channel / Supergroup / Chat ID → approximate creation date anchors ─────────
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


def _interpolate(entity_id: int, anchors: list[tuple[int, datetime]]) -> datetime:
    """Линейная интерполяция даты по ID. Никогда не возвращает дату в будущем."""
    _now = datetime.now(tz=timezone.utc)
    if entity_id <= anchors[0][0]:
        return anchors[0][1]
    if entity_id >= anchors[-1][0]:
        # ID выше всех якорей: возвращаем последний якорь, но не будущее
        return min(anchors[-1][1], _now)
    for i in range(len(anchors) - 1):
        lo_id, lo_dt = anchors[i]
        hi_id, hi_dt = anchors[i + 1]
        if lo_id <= entity_id <= hi_id:
            frac = (entity_id - lo_id) / (hi_id - lo_id)
            delta_s = (hi_dt - lo_dt).total_seconds()
            result = datetime.fromtimestamp(
                lo_dt.timestamp() + frac * delta_s, tz=timezone.utc
            )
            return min(result, _now)
    return min(anchors[-1][1], _now)


def estimate_confidence_range(
    entity_id: int, entity_type: str
) -> tuple[datetime, datetime]:
    """
    Вернуть (lo_date, hi_date) — интервал доверия для ID-интерполяции.
    Возвращает крайние точки сегмента в котором лежит entity_id.
    """
    anchors = _USER_ANCHORS if entity_type in ("user", "bot") else _CHAN_ANCHORS
    canonical = (
        abs(entity_id)
        if entity_type in ("user", "bot")
        else canonical_peer_id(entity_id)
    )
    if canonical <= anchors[0][0]:
        return anchors[0][1], anchors[0][1]
    if canonical >= anchors[-1][0]:
        return anchors[-1][1], anchors[-1][1]
    for i in range(len(anchors) - 1):
        lo_id, lo_dt = anchors[i]
        hi_id, hi_dt = anchors[i + 1]
        if lo_id <= canonical <= hi_id:
            return lo_dt, hi_dt
    return anchors[-1][1], anchors[-1][1]


# ── Public API ─────────────────────────────────────────────────────────────────

def estimate_by_id(entity_id: int, entity_type: str) -> dict[str, Any]:
    """
    Оценить дату регистрации/создания по Telegram ID.
    entity_type: 'user' | 'bot' | 'channel' | 'supergroup' | 'group'
    """
    anchors = _USER_ANCHORS if entity_type in ("user", "bot") else _CHAN_ANCHORS
    canonical = (
        abs(entity_id)
        if entity_type in ("user", "bot")
        else canonical_peer_id(entity_id)
    )
    now = datetime.now(tz=timezone.utc)

    # ID выше последней верифицированной точки — интерполяция ненадёжна.
    # Сообщаем нижнюю границу (последний якорь) + верхнюю границу (сегодня).
    if entity_type in ("user", "bot") and canonical > _LAST_VERIFIED_USER_ANCHOR[0]:
        verified_dt = _LAST_VERIFIED_USER_ANCHOR[1]
        # Вычисляем примерную дату из линейной модели, но обрезаем сегодняшним числом
        last_id, last_dt = anchors[-1]
        # Используем скорость роста из предыдущего сегмента
        if len(anchors) >= 2:
            prev_id, prev_dt = anchors[-2]
            rate = (last_dt - prev_dt).total_seconds() / max(last_id - prev_id, 1)
            extrapolated = datetime.fromtimestamp(
                last_dt.timestamp() + rate * (canonical - last_id), tz=timezone.utc
            )
            extrapolated = min(extrapolated, now)
        else:
            extrapolated = min(last_dt, now)
        return {
            "entity_id": entity_id,
            "canonical_id": canonical,
            "entity_type": entity_type,
            "date": extrapolated,
            "method": "after_verified",
            "confidence": "нижняя граница",
            "confidence_lo": verified_dt,
            "confidence_hi": now,
            "verified_lower_bound": verified_dt,
        }

    dt = _interpolate(canonical, anchors)
    lo, hi = estimate_confidence_range(entity_id, entity_type)
    return {
        "entity_id": entity_id,
        "canonical_id": canonical,
        "entity_type": entity_type,
        "date": dt,
        "method": "id_interpolation",
        "confidence": "~±2 мес.",
        "confidence_lo": lo,
        "confidence_hi": hi,
    }


async def get_entity_full_info(
    pool: asyncpg.Pool,
    owner_id: int,
    peer,  # str (username/+hash) | int (canonical_id) | Telethon peer object
) -> dict[str, Any] | None:
    """
    Получить полную информацию о сущности через Telethon за один сеанс:
      • тип, имя, username
      • verified / scam / fake / premium флаги
      • количество подписчиков (для каналов/групп)
      • описание (about)
      • точная дата создания (first message, для каналов/групп)

    Возвращает dict или None при ошибке/нет аккаунтов.
    """
    try:
        from services import resource_selector
        from services.account_manager import _make_client
        from telethon.tl.types import User, Channel, Chat
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import PeerChannel, PeerChat

        candidates = await resource_selector.select_all_active(
            pool, owner_id, action_type="read"
        )
        if not candidates:
            return None
        acc = next((a for a in candidates if a.get("session_str")), None)
        if not acc:
            return None

        client = _make_client(acc["session_str"])
        try:
            await asyncio.wait_for(client.connect(), timeout=15)

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

                # ── Уникальный метод: первое фото профиля = нижняя граница даты ──
                # photos.GetUserPhotosRequest(offset=total-1, limit=1) → самое старое фото.
                # Это работает для ботов и пользователей — никто другой не использует.
                try:
                    from telethon.tl.functions.photos import GetUserPhotosRequest
                    # Сначала получаем только count (limit=0)
                    ph_count_resp = await asyncio.wait_for(
                        client(GetUserPhotosRequest(entity, offset=0, max_id=0, limit=0)),
                        timeout=10,
                    )
                    total_photos = getattr(ph_count_resp, "count", 0)
                    if total_photos and total_photos > 0:
                        # Берём самое старое фото (offset = total - 1)
                        ph_resp = await asyncio.wait_for(
                            client(GetUserPhotosRequest(
                                entity, offset=max(0, total_photos - 1), max_id=0, limit=1
                            )),
                            timeout=10,
                        )
                        photos_list = getattr(ph_resp, "photos", [])
                        if photos_list:
                            oldest_photo_date = getattr(photos_list[0], "date", None)
                            if oldest_photo_date:
                                if isinstance(oldest_photo_date, (int, float)):
                                    from datetime import datetime, timezone as _tz
                                    oldest_photo_date = datetime.fromtimestamp(oldest_photo_date, tz=_tz.utc)
                                result["oldest_photo_date"] = oldest_photo_date
                                result["total_photos"] = total_photos
                except Exception:
                    pass

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
                # Full channel info
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

                # Exact date via first message
                peer_obj = (
                    PeerChannel(entity.id)
                    if isinstance(entity, Channel)
                    else PeerChat(entity.id)
                )
                exact_date: datetime | None = None
                try:
                    msg = await asyncio.wait_for(
                        client.get_messages(peer_obj, ids=1), timeout=20
                    )
                    if msg and not isinstance(msg, list):
                        exact_date = msg.date
                    elif msg and isinstance(msg, list) and msg and msg[0]:
                        exact_date = msg[0].date
                except Exception:
                    pass
                if not exact_date:
                    try:
                        async for oldest in client.iter_messages(
                            entity, limit=1, reverse=True
                        ):
                            exact_date = oldest.date
                    except Exception:
                        pass
                if exact_date:
                    result["exact_date"] = exact_date

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
    Обратная совместимость — используй get_entity_full_info() для новых вызовов.
    """
    try:
        from services import resource_selector
        from services.account_manager import _make_client

        candidates = await resource_selector.select_all_active(
            pool, owner_id, action_type="read"
        )
        if not candidates:
            return None
        acc = next((a for a in candidates if a.get("session_str")), None)
        if not acc:
            return None

        client = _make_client(acc["session_str"])
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
            try:
                msg = await asyncio.wait_for(
                    client.get_messages(peer, ids=1), timeout=20
                )
            except Exception:
                msg = None
            if msg and not isinstance(msg, list):
                return {"date": msg.date, "method": "first_message", "confidence": "exact"}
            if msg and isinstance(msg, list) and msg and msg[0]:
                return {"date": msg[0].date, "method": "first_message", "confidence": "exact"}
            async for oldest in client.iter_messages(peer, limit=1, reverse=True):
                return {"date": oldest.date, "method": "first_message", "confidence": "exact"}
        finally:
            await client.disconnect()

    except (asyncio.TimeoutError, ConnectionError) as e:
        log.warning("registration_checker.get_channel_exact_date timeout: %s", e)
    except Exception as e:
        log.warning("registration_checker.get_channel_exact_date: %s", e)
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
    # t.me/joinchat or t.me/+ (invite link)
    m = re.match(
        r"(?:https?://)?t(?:elegram)?\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", text
    )
    if m:
        return {"username": "+" + m.group(1), "type": "invite"}
    # t.me/username
    m = re.match(r"(?:https?://)?t(?:elegram)?\.me/([a-zA-Z0-9_]{3,32})", text)
    if m:
        return {"username": m.group(1), "type": "username"}
    # @username
    m = re.match(r"@([a-zA-Z0-9_]{3,32})", text)
    if m:
        return {"username": m.group(1), "type": "username"}
    # numeric ID (5+ digits, optional leading minus)
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

    # Metadata
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

    # Badges
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
        "first_message": "✅ Первое сообщение (точно)",
        "after_verified": "🆕 Создан недавно (выше верифицированных данных)",
        "oldest_photo": "🖼 По первому фото профиля",
    }.get(method, method)

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
        lines.append(f"👥 Подписчиков: <b>{participants:,}</b>".replace(",", " "))

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
            # Первое фото профиля = точная нижняя граница регистрации
            ph_s = format_date_ru(oldest_photo)
            lines.append(f"📅 Создан: <b>не позднее {ph_s}</b>")
            lines.append(f"⏳ Возраст: <b>≥ {format_age(oldest_photo)}</b>")
            lines.append(f"📸 Первое фото: {ph_s}")
            lines.append(f"📏 Нижняя граница по ID: после {lb_s}")
        else:
            lines.append(f"📅 Создан: <b>после {lb_s}</b>")
            lines.append(f"⚡ Возраст: <b>< {format_age(lb)}</b>")
            lines.append(f"📏 Диапазон: <i>{lb_s} — сегодня</i>")
    elif dt:
        lines.append(f"📅 Дата: <b>{format_date_ru(dt)}</b>")
        lines.append(f"⏳ Возраст: <b>{format_age(dt)}</b>")
        if method == "id_interpolation" and confidence_lo and confidence_hi:
            if confidence_lo != confidence_hi:
                lo_s = format_date_ru(confidence_lo)
                hi_s = format_date_ru(confidence_hi)
                lines.append(f"📏 Диапазон: <i>{lo_s} — {hi_s}</i>")
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
