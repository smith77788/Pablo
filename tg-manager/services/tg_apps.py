"""Пул Telegram-приложений (api_id/api_hash) и стабильное назначение по аккаунтам.

Первопричина: весь флот подключался под ОДНОЙ парой TG_API_ID/TG_API_HASH.
Для Telegram это прямой корреляционный признак — все аккаунты видны как клиенты
одного приложения, независимо от разных прокси, устройств и гео. Это самый
сильный оставшийся сигнал связности когорты.

Решение: оператор задаёт пул приложений (env `TG_API_POOL`), аккаунты
распределяются по нему СТАБИЛЬНО:
  • если у аккаунта уже записан api_id (tg_accounts.api_id) и он есть в пуле —
    используется он (смена приложения у живого аккаунта сама по себе палевна);
  • иначе — детерминированно от account_id (rendezvous-хеширование: при
    ДОБАВЛЕНИИ приложения в пул переезжает лишь ~1/N аккаунтов, а не все).

Пул пуст / не задан → поведение ровно прежнее (одна пара из config), без
регрессии для существующих инсталляций.

Формат `TG_API_POOL`: "12345:hash1,67890:hash2" (разделители , или ;).
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_ENV = "TG_API_POOL"


def _default_pair() -> Optional[tuple[int, str]]:
    try:
        from config import TG_API_ID, TG_API_HASH
    except Exception:  # pragma: no cover — config всегда есть в проде
        return None
    try:
        aid = int(TG_API_ID or 0)
    except (TypeError, ValueError):
        return None
    if aid and TG_API_HASH:
        return (aid, str(TG_API_HASH))
    return None


def parse_pool(raw: str | None) -> list[tuple[int, str]]:
    """Разобрать строку пула. Кривые записи пропускаются (fail-soft)."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        sid, _, shash = chunk.partition(":")
        sid, shash = sid.strip(), shash.strip()
        if not sid.isdigit() or not shash:
            continue
        aid = int(sid)
        if aid in seen:
            continue
        seen.add(aid)
        out.append((aid, shash))
    return out


def pool() -> list[tuple[int, str]]:
    """Актуальный пул приложений: env + дефолтная пара из config."""
    apps = parse_pool(os.getenv(_ENV))
    dflt = _default_pair()
    if dflt and dflt[0] not in {a for a, _ in apps}:
        apps.append(dflt)
    return apps


def _score(key: str, api_id: int) -> str:
    """Вес пары (ключ аккаунта, приложение) для rendezvous-хеширования."""
    return hashlib.sha256(f"{key}:{api_id}".encode()).hexdigest()


def for_account(
    account_id: "int | str | None",
    stored_api_id: Optional[int] = None,
    apps: Optional[list[tuple[int, str]]] = None,
) -> Optional[tuple[int, str]]:
    """Пара (api_id, api_hash) для аккаунта. None → вызывающий берёт дефолт.

    Приоритет: закреплённый за аккаунтом api_id → детерминированный выбор по
    account_id → дефолт.
    """
    apps = pool() if apps is None else apps
    if not apps:
        return None
    if stored_api_id:
        try:
            sid = int(stored_api_id)
        except (TypeError, ValueError):
            sid = 0
        for aid, ahash in apps:
            if aid == sid:
                return (aid, ahash)
    # Ключом может быть id аккаунта ИЛИ любая стабильная строка (например
    # fingerprint сессии) — при импорте id ещё не существует, а распределять
    # приложения уже нужно, иначе вся пачка сядет на одно приложение.
    key = "" if account_id is None else str(account_id).strip()
    if not key:
        return apps[0]
    # Rendezvous (HRW): максимальный вес. Добавление приложения переносит ~1/N.
    return max(apps, key=lambda p: _score(key, p[0]))


def assign_api_id(key: "int | str | None") -> Optional[int]:
    """api_id для закрепления за НОВЫМ аккаунтом (импорт/регистрация).

    `key` — любой стабильный уникальный идентификатор аккаунта: id, если он уже
    есть, иначе fingerprint сессии/телефон. None → первое приложение пула.
    """
    pair = for_account(key)
    return pair[0] if pair else None
