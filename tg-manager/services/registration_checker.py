"""
Проверка даты регистрации пользователей и даты создания каналов/групп/ботов.

Два метода:
  1. ID-интерполяция — для всех типов сущностей, быстро, погрешность ~±2 мес.
  2. Telethon first-message — для каналов/групп, точная дата, требует аккаунт.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

# ── User / Bot ID → approximate registration date anchors ────────────────────
# Источник: верифицировано по известным публичным аккаунтам.
_USER_ANCHORS: list[tuple[int, datetime]] = [
    (1,             datetime(2013, 8, 14, tzinfo=timezone.utc)),
    (10_000_000,    datetime(2013, 10, 1, tzinfo=timezone.utc)),
    (50_000_000,    datetime(2014, 3, 1,  tzinfo=timezone.utc)),
    (100_000_000,   datetime(2014, 7, 1,  tzinfo=timezone.utc)),
    (200_000_000,   datetime(2015, 1, 1,  tzinfo=timezone.utc)),
    (300_000_000,   datetime(2015, 8, 1,  tzinfo=timezone.utc)),
    (400_000_000,   datetime(2016, 3, 1,  tzinfo=timezone.utc)),
    (500_000_000,   datetime(2016, 9, 1,  tzinfo=timezone.utc)),
    (600_000_000,   datetime(2017, 3, 1,  tzinfo=timezone.utc)),
    (700_000_000,   datetime(2017, 9, 1,  tzinfo=timezone.utc)),
    (800_000_000,   datetime(2018, 3, 1,  tzinfo=timezone.utc)),
    (900_000_000,   datetime(2018, 9, 1,  tzinfo=timezone.utc)),
    (1_000_000_000, datetime(2019, 3, 1,  tzinfo=timezone.utc)),
    (1_100_000_000, datetime(2019, 7, 1,  tzinfo=timezone.utc)),
    (1_200_000_000, datetime(2019, 10, 1, tzinfo=timezone.utc)),
    (1_300_000_000, datetime(2020, 1, 1,  tzinfo=timezone.utc)),
    (1_400_000_000, datetime(2020, 4, 1,  tzinfo=timezone.utc)),
    (1_500_000_000, datetime(2020, 7, 1,  tzinfo=timezone.utc)),
    (1_600_000_000, datetime(2020, 10, 1, tzinfo=timezone.utc)),
    (1_700_000_000, datetime(2021, 1, 1,  tzinfo=timezone.utc)),
    (1_800_000_000, datetime(2021, 5, 1,  tzinfo=timezone.utc)),
    (1_900_000_000, datetime(2021, 9, 1,  tzinfo=timezone.utc)),
    (2_000_000_000, datetime(2021, 12, 1, tzinfo=timezone.utc)),
    (3_000_000_000, datetime(2022, 5, 1,  tzinfo=timezone.utc)),
    (4_000_000_000, datetime(2022, 9, 1,  tzinfo=timezone.utc)),
    (5_000_000_000, datetime(2023, 1, 1,  tzinfo=timezone.utc)),
    (6_000_000_000, datetime(2023, 5, 1,  tzinfo=timezone.utc)),
    (7_000_000_000, datetime(2023, 9, 1,  tzinfo=timezone.utc)),
    (8_000_000_000, datetime(2024, 2, 1,  tzinfo=timezone.utc)),
    (9_000_000_000, datetime(2024, 8, 1,  tzinfo=timezone.utc)),
    (10_000_000_000, datetime(2025, 2, 1, tzinfo=timezone.utc)),
]

# ── Channel / Supergroup / Chat ID → approximate creation date anchors ────────
_CHAN_ANCHORS: list[tuple[int, datetime]] = [
    (1,             datetime(2013, 9, 1,  tzinfo=timezone.utc)),
    (1_000_000,     datetime(2014, 2, 1,  tzinfo=timezone.utc)),
    (10_000_000,    datetime(2014, 9, 1,  tzinfo=timezone.utc)),
    (50_000_000,    datetime(2015, 5, 1,  tzinfo=timezone.utc)),
    (100_000_000,   datetime(2015, 12, 1, tzinfo=timezone.utc)),
    (200_000_000,   datetime(2016, 7, 1,  tzinfo=timezone.utc)),
    (300_000_000,   datetime(2017, 3, 1,  tzinfo=timezone.utc)),
    (400_000_000,   datetime(2017, 9, 1,  tzinfo=timezone.utc)),
    (500_000_000,   datetime(2018, 4, 1,  tzinfo=timezone.utc)),
    (600_000_000,   datetime(2018, 10, 1, tzinfo=timezone.utc)),
    (700_000_000,   datetime(2019, 4, 1,  tzinfo=timezone.utc)),
    (800_000_000,   datetime(2019, 9, 1,  tzinfo=timezone.utc)),
    (900_000_000,   datetime(2020, 1, 1,  tzinfo=timezone.utc)),
    (1_000_000_000, datetime(2020, 6, 1,  tzinfo=timezone.utc)),
    (1_200_000_000, datetime(2020, 12, 1, tzinfo=timezone.utc)),
    (1_400_000_000, datetime(2021, 6, 1,  tzinfo=timezone.utc)),
    (1_600_000_000, datetime(2022, 1, 1,  tzinfo=timezone.utc)),
    (1_800_000_000, datetime(2022, 7, 1,  tzinfo=timezone.utc)),
    (2_000_000_000, datetime(2023, 1, 1,  tzinfo=timezone.utc)),
    (2_200_000_000, datetime(2023, 7, 1,  tzinfo=timezone.utc)),
    (2_400_000_000, datetime(2024, 1, 1,  tzinfo=timezone.utc)),
    (2_600_000_000, datetime(2024, 7, 1,  tzinfo=timezone.utc)),
    (2_800_000_000, datetime(2025, 1, 1,  tzinfo=timezone.utc)),
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
    """Линейная интерполяция даты по ID между ближайшими опорными точками."""
    if entity_id <= anchors[0][0]:
        return anchors[0][1]
    if entity_id >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        lo_id, lo_dt = anchors[i]
        hi_id, hi_dt = anchors[i + 1]
        if lo_id <= entity_id <= hi_id:
            frac = (entity_id - lo_id) / (hi_id - lo_id)
            delta_s = (hi_dt - lo_dt).total_seconds()
            return datetime.fromtimestamp(
                lo_dt.timestamp() + frac * delta_s, tz=timezone.utc
            )
    return anchors[-1][1]


# ── Public API ─────────────────────────────────────────────────────────────────

def estimate_by_id(entity_id: int, entity_type: str) -> dict[str, Any]:
    """
    Оценить дату регистрации/создания по Telegram ID.
    entity_type: 'user' | 'bot' | 'channel' | 'supergroup' | 'group'
    """
    anchors = _USER_ANCHORS if entity_type in ("user", "bot") else _CHAN_ANCHORS
    canonical = canonical_peer_id(entity_id) if entity_type not in ("user", "bot") else abs(entity_id)
    dt = _interpolate(canonical, anchors)
    return {
        "entity_id": entity_id,
        "canonical_id": canonical,
        "entity_type": entity_type,
        "date": dt,
        "method": "id_interpolation",
        "confidence": "~±2 мес.",
    }


async def get_channel_exact_date(
    pool: asyncpg.Pool,
    owner_id: int,
    peer,  # username str | int peer_id | Telethon peer object
) -> dict[str, Any] | None:
    """
    Получить точную дату создания канала/группы через первое сообщение.
    Использует Telethon с аккаунтом из пула пользователя.
    Возвращает dict или None при ошибке/нет аккаунтов.
    """
    try:
        from services import resource_selector
        from services.account_manager import _make_client

        candidates = await resource_selector.select_all_active(
            pool, owner_id, action_type="read"
        )
        if not candidates:
            return None

        acc = next(
            (a for a in candidates if a.get("session_str")), None
        )
        if not acc:
            return None

        client = _make_client(acc["session_str"])
        try:
            await asyncio.wait_for(client.connect(), timeout=15)

            # Try message with ID=1 (created by Telegram automatically)
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

            # Fallback: oldest message via reverse iteration
            async for oldest in client.iter_messages(peer, limit=1, reverse=True):
                return {"date": oldest.date, "method": "first_message", "confidence": "exact"}

        finally:
            await client.disconnect()

    except (asyncio.TimeoutError, ConnectionError) as e:
        log.warning("registration_checker: connect timeout: %s", e)
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
    Возвращает {'entity_id', 'entity_type', 'name', 'username'} или None.
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

        clean = username.lstrip("@").strip()
        # Build t.me link for invite hashes
        if re.match(r"^[a-zA-Z0-9_]{20,}$", clean) or clean.startswith("+"):
            peer_arg = f"https://t.me/{clean}"
        else:
            peer_arg = clean

        client = _make_client(acc["session_str"])
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
            entity = await asyncio.wait_for(client.get_entity(peer_arg), timeout=20)
        finally:
            await client.disconnect()

        from telethon.tl.types import User, Channel, Chat

        if isinstance(entity, User):
            return {
                "entity_id": entity.id,
                "entity_type": "bot" if entity.bot else "user",
                "name": (entity.first_name or "") + (" " + entity.last_name if entity.last_name else ""),
                "username": entity.username,
            }
        elif isinstance(entity, (Channel, Chat)):
            chan_type = "supergroup" if getattr(entity, "megagroup", False) else "channel"
            return {
                "entity_id": entity.id,
                "entity_type": chan_type,
                "name": entity.title or "",
                "username": getattr(entity, "username", None),
            }

    except Exception as e:
        log.warning("registration_checker.resolve_username(%s): %s", username, e)
    return None


async def cache_result(
    pool: asyncpg.Pool,
    owner_id: int,
    result: dict[str, Any],
    name: str | None,
    username: str | None,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO reg_check_cache
               (entity_id, entity_type, entity_name, username, reg_date, method, checked_by)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (entity_id, entity_type) DO UPDATE
               SET entity_name=$3, username=$4, reg_date=$5, method=$6,
                   checked_by=$7, checked_at=NOW()""",
            result["entity_id"],
            result["entity_type"],
            name,
            username,
            result.get("date"),
            result.get("method", "id_interpolation"),
            owner_id,
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
    m = re.match(r"(?:https?://)?t(?:elegram)?\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)", text)
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
    # numeric ID
    m = re.match(r"^-?\d{5,}$", text)
    if m:
        return {"username": text, "type": "id"}
    return None


def format_date_ru(dt: datetime) -> str:
    date_str = dt.strftime("%-d %B %Y")
    for en, ru in _RU_MONTHS.items():
        date_str = date_str.replace(en, ru)
    return date_str


def format_age(dt: datetime) -> str:
    now = datetime.now(tz=timezone.utc)
    days = (now - dt).days
    years, rem = divmod(days, 365)
    months = rem // 30
    if years > 0 and months > 0:
        return f"{years} лет {months} мес."
    if years > 0:
        return f"{years} лет"
    return f"{months} мес."


def format_result(result: dict[str, Any], name: str | None = None, username: str | None = None) -> str:
    entity_id = result.get("entity_id", "?")
    entity_type = result.get("entity_type", "unknown")
    dt: datetime | None = result.get("date")
    method = result.get("method", "id_interpolation")
    confidence = result.get("confidence", "")

    type_icons = {
        "user": "👤", "bot": "🤖",
        "channel": "📢", "supergroup": "👥 Супергруппа", "group": "👥 Группа",
    }
    type_labels = {
        "user": "Пользователь", "bot": "Бот",
        "channel": "Канал", "supergroup": "Супергруппа", "group": "Группа",
    }
    icon = type_icons.get(entity_type, "❓")
    label = type_labels.get(entity_type, entity_type.capitalize())

    method_label = {
        "id_interpolation": "📊 Оценка по ID",
        "first_message": "✅ Первое сообщение (точно)",
    }.get(method, method)

    lines = [f"{icon} <b>{label}</b>"]
    if name:
        lines.append(f"🏷 <b>{name}</b>")
    if username:
        lines.append(f"🔗 @{username}")
    lines.append(f"🔢 ID: <code>{entity_id}</code>")
    lines.append("")

    if dt:
        lines.append(f"📅 Дата: <b>{format_date_ru(dt)}</b>")
        lines.append(f"⏳ Возраст: <b>{format_age(dt)}</b>")
    else:
        lines.append("📅 Дата: <i>не удалось определить</i>")

    lines.append(f"🔍 Метод: {method_label}")
    if confidence and confidence not in ("exact",):
        lines.append(f"📏 Точность: <i>{confidence}</i>")

    return "\n".join(lines)
