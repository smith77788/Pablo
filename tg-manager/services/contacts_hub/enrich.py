"""Авто-обогащение контакта: производные признаки без сети.

Синк тянет сырые поля; здесь — чистые эвристики поверх них: уровень активности
(из last_seen) и предполагаемый язык (по письменности имени). Детерминированно,
тестируется без БД/Telegram. Используется как обогащение «на чтение» (в карточке),
без записи в синк-путь — значит, безопасно по дубликату сессии.
"""
from __future__ import annotations

import re

# Активность по типу «был в сети» (как отдаёт Telegram-статус).
_ACTIVITY = {
    "online": ("hot", "🟢 Активен"),
    "recently": ("hot", "Активен (недавно)"),
    "last_week": ("warm", "Заходит на неделе"),
    "last_month": ("cold", "Редко (в этом месяце)"),
    "offline": ("cold", "Давно не заходил"),
}

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[一-鿿぀-ヿ가-힯]")
_ARABIC = re.compile(r"[؀-ۿ]")


def activity_level(last_seen_type: str | None, is_premium: bool = False) -> dict:
    """Уровень активности контакта: {level, label}. Premium слегка «теплее»."""
    level, label = _ACTIVITY.get((last_seen_type or "").lower(), ("unknown", "Неизвестно"))
    if level == "cold" and is_premium:
        level = "warm"  # премиум обычно живее — небольшая поправка
    return {"level": level, "label": label}


def guess_language(first_name: str = "", last_name: str = "") -> dict:
    """Предположить язык по письменности имени: {code, confidence}.

    Грубая эвристика (не детект по тексту сообщений): преобладающая письменность
    имени → язык. confidence низкая по умолчанию — это подсказка, не факт.
    """
    text = f"{first_name or ''} {last_name or ''}"
    counts = {
        "ru": len(_CYRILLIC.findall(text)),
        "en": len(_LATIN.findall(text)),
        "cjk": len(_CJK.findall(text)),
        "ar": len(_ARABIC.findall(text)),
    }
    total = sum(counts.values())
    if total == 0:
        return {"code": None, "confidence": 0.0}
    code = max(counts, key=counts.get)
    confidence = round(counts[code] / total, 2)
    # Латиница слабо различает языки — понижаем уверенность.
    if code == "en":
        confidence = round(confidence * 0.6, 2)
    return {"code": code, "confidence": confidence}


def enrich(contact: dict) -> dict:
    """Собрать производные признаки для карточки контакта."""
    return {
        "activity": activity_level(contact.get("last_seen_type"),
                                   bool(contact.get("is_premium"))),
        "language": guess_language(contact.get("first_name") or "",
                                   contact.get("last_name") or ""),
    }
