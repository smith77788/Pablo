"""Массовые локальные атрибуты аккаунтов (без Telegram): версии/пол/роль.

Три действия из панели аккаунтов, которые НЕ ходят в Telegram, а меняют локальные
поля tg_accounts — поэтому синхронные (без очереди/канарейки, мгновенный результат):

- regenerate_versions — перегенерировать device-fingerprint (device_model/
  system_version/app_version/lang_code) через account_manager.generate_device_fingerprint
  («Исправить версии в JSON»); учитывает geo_country аккаунта для локали;
- set_gender — проставить пол 'm'|'f' (локальная категоризация оператора);
- add_role / remove_role — роль = тег в tg_accounts.tags[] (add/remove, идемпотентно).

Все функции скоупятся по owner_id (защита от чужих аккаунтов) и возвращают число
затронутых аккаунтов.
"""
from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)


async def regenerate_versions(
    pool: asyncpg.Pool, owner_id: int, account_ids: list[int]
) -> int:
    """Перегенерировать device-fingerprint у выбранных аккаунтов (каждому — свой).

    Random на аккаунт → одинаковой сигнатуры у пачки нет (анти-детект). Локаль
    берём из geo_country аккаунта, если известна.
    """
    from services.account_manager import generate_device_fingerprint

    if not account_ids:
        return 0
    rows = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        owner_id, [int(i) for i in account_ids])
    n = 0
    for r in rows:
        fp = generate_device_fingerprint()
        await pool.execute(
            "UPDATE tg_accounts SET device_model=$2, system_version=$3, app_version=$4, "
            "lang_code=$5, system_lang_code=$6 WHERE id=$1 AND owner_id=$7",
            r["id"], fp["device_model"], fp["system_version"], fp["app_version"],
            fp["lang_code"], fp["system_lang_code"], owner_id)
        n += 1
    log.info("account_bulk_attrs.regenerate_versions owner=%s → %d", owner_id, n)
    return n


async def set_gender(
    pool: asyncpg.Pool, owner_id: int, account_ids: list[int], gender: str
) -> int:
    """Проставить пол аккаунтам ('m'|'f'). Иное значение отвергаем."""
    if gender not in ("m", "f"):
        raise ValueError("пол должен быть 'm' или 'f'")
    if not account_ids:
        return 0
    res = await pool.execute(
        "UPDATE tg_accounts SET gender=$3 WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        owner_id, [int(i) for i in account_ids], gender)
    n = int(str(res).split()[-1]) if str(res).startswith("UPDATE") else 0
    log.info("account_bulk_attrs.set_gender owner=%s g=%s → %d", owner_id, gender, n)
    return n


async def add_role(
    pool: asyncpg.Pool, owner_id: int, account_ids: list[int], role: str
) -> int:
    """Добавить роль (тег) аккаунтам. Идемпотентно: дубля в tags не создаёт."""
    role = (role or "").strip()
    if not role:
        raise ValueError("пустая роль")
    if not account_ids:
        return 0
    # array_append только если тега ещё нет — иначе дубли в tags[].
    res = await pool.execute(
        "UPDATE tg_accounts SET tags = array_append(COALESCE(tags,'{}'), $3) "
        "WHERE owner_id=$1 AND id = ANY($2::bigint[]) "
        "AND NOT ($3 = ANY(COALESCE(tags,'{}')))",
        owner_id, [int(i) for i in account_ids], role)
    n = int(str(res).split()[-1]) if str(res).startswith("UPDATE") else 0
    log.info("account_bulk_attrs.add_role owner=%s role=%s → %d", owner_id, role, n)
    return n


async def remove_role(
    pool: asyncpg.Pool, owner_id: int, account_ids: list[int], role: str
) -> int:
    """Убрать роль (тег) у аккаунтов."""
    role = (role or "").strip()
    if not role:
        raise ValueError("пустая роль")
    if not account_ids:
        return 0
    res = await pool.execute(
        "UPDATE tg_accounts SET tags = array_remove(COALESCE(tags,'{}'), $3) "
        "WHERE owner_id=$1 AND id = ANY($2::bigint[]) "
        "AND $3 = ANY(COALESCE(tags,'{}'))",
        owner_id, [int(i) for i in account_ids], role)
    n = int(str(res).split()[-1]) if str(res).startswith("UPDATE") else 0
    log.info("account_bulk_attrs.remove_role owner=%s role=%s → %d", owner_id, role, n)
    return n
