"""Жизненный цикл сессии аккаунта WB Chat поверх транспорта.

Аналог account_manager._make_client + прогрева сессии: поднять транспорт из
сохранённой (расшифрованной) сессии аккаунта, подключиться, проверить авторизацию
и обновить «здоровье»/статус в БД. Верхние слои (op_worker, движки) получают уже
готовый транспорт и не знают, какой драйвер активен.
"""

from __future__ import annotations

import logging

import asyncpg

from services.wb_chat import accounts
from services.wb_chat.transport import (
    WBChatTransport,
    WBProtocolUnavailable,
    build_transport,
)

log = logging.getLogger(__name__)


def open_transport(account: dict, *, driver: str | None = None) -> WBChatTransport:
    """Создать (не подключая) транспорт из аккаунта. connect() — на вызывающем.

    Секреты в account уже расшифрованы (accounts._row_to_account)."""
    return build_transport(
        session=account.get("session") or None,
        proxy=account.get("proxy") or None,
        device=account.get("device") or None,
        driver=driver,
    )


async def validate(pool: asyncpg.Pool, account: dict, *, driver: str | None = None) -> bool:
    """Проверить сессию аккаунта (connect → is_authorized). Обновить статус в БД.

    True — аккаунт рабочий; False — сессия невалидна (помечаем 'invalid'). При
    неподнятом реальном протоколе пробрасываем WBProtocolUnavailable (это не
    «аккаунт плохой», а «транспорт не готов» — не глушим)."""
    transport = open_transport(account, driver=driver)
    try:
        async with transport:
            ok = await transport.is_authorized()
    except WBProtocolUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — сетевые/сессионные сбои → аккаунт под вопросом
        log.warning("wb_chat: валидация аккаунта %s не удалась: %s", account.get("id"), e)
        await accounts.set_status(pool, account["id"], "invalid", health_delta=-15)
        return False

    if ok:
        await accounts.set_status(pool, account["id"], "active", health_delta=+5)
        return True
    await accounts.set_status(pool, account["id"], "invalid", health_delta=-20)
    return False
