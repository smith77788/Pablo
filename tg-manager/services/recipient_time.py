"""Время ПОЛУЧАТЕЛЯ сообщения: часовой пояс человека и «сейчас» у него. ЧИСТЫЙ.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Подстановки {{DATE}}/{{TIME}} в текстах ботов
(сценарии, рассылки, автоответы) брали `datetime.now()` — время СЕРВЕРА, а
сервер живёт в UTC. Человек из Москвы в 21:30 получал «сейчас 18:30», а после
21:00 по Москве — ещё и вчерашнюю дату. Правило «какой пояс у человека» уже
было, но жило внутри исполнителя сценариев (умная задержка), и три места
рендера текста знать о нём не могли. Теперь правило одно и лежит здесь.

Часовой пояс человека Telegram не сообщает. Берём по порядку: поле контакта
`timezone` (сценарий может спросить), язык клиента Telegram (ru — Москва,
uk — Киев …), запасной пояс вызывающего, Москва.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LANG_TZ = {
    "ru": "Europe/Moscow", "uk": "Europe/Kyiv", "be": "Europe/Minsk",
    "kk": "Asia/Almaty", "uz": "Asia/Tashkent", "ky": "Asia/Bishkek",
    "hy": "Asia/Yerevan", "ka": "Asia/Tbilisi", "az": "Asia/Baku",
    "tg": "Asia/Dushanbe", "pl": "Europe/Warsaw", "de": "Europe/Berlin",
    "lv": "Europe/Riga", "lt": "Europe/Vilnius", "et": "Europe/Tallinn",
}
DEFAULT_TZ = "Europe/Moscow"


def zone(name: str | None):
    """ZoneInfo по имени или None, если имя пустое/неизвестное."""
    if not name:
        return None
    try:
        return ZoneInfo(str(name).strip())
    except Exception:
        return None


def contact_tz(fields: dict | None, language_code: str | None,
               fallback_tz: str | None = None) -> str:
    """IANA-зона человека (см. порядок в докстринге модуля)."""
    own = (fields or {}).get("timezone")
    if own and zone(own):
        return str(own).strip()
    lang = (language_code or "").split("-")[0].strip().lower()
    if lang in LANG_TZ:
        return LANG_TZ[lang]
    if fallback_tz and zone(fallback_tz):
        return fallback_tz
    return DEFAULT_TZ


def now_for(fields: dict | None = None, language_code: str | None = None,
            now_utc: datetime | None = None) -> datetime:
    """«Сейчас» в поясе человека (tz-aware)."""
    base = now_utc or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(zone(contact_tz(fields, language_code)) or ZoneInfo(DEFAULT_TZ))
