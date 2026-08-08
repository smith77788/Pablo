"""Единый асинхронный «предохранитель» вокруг сетевых вызовов Telethon.

Стандарт обработки ошибок (Стадия 2, пункт #3) в одном месте, вместо разнородных
try/except в каждом движке:

  • FloodWait  → безопасный client.disconnect() и **handoff вверх** (raise
    FloodHandoff со сроком) — НЕ усыпляем текущий поток; оператор/исполнитель
    ставит аккаунт на cooldown и передаёт задачу другому.
  • Transient  → ретрай с экспоненциальным бэкоффом + джиттер (сеть/таймаут/
    временный сбой соединения).
  • Critical   → немедленный проброс (протухшая сессия, спам-блок, отказ прав) —
    ретраить бессмысленно и вредно.

Классификация — по имени класса исключения и атрибутам (без жёстких импортов
telethon), поэтому модуль импортируется и тестируется без сети и без telethon.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# Транзиентные (стоит ретраить) — по подстроке имени класса исключения.
_TRANSIENT_NAMES = (
    "TimeoutError", "ConnectionError", "ConnectionResetError", "OSError",
    "ServerError", "RpcCallFailError", "RpcMcgetFailError", "TimedOutError",
    "DisconnectError", "ConnectionAbortedError",
)
# Критичные (ретрай вреден/бесполезен) — сессия/права/спам-блок.
_CRITICAL_NAMES = (
    "AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyDuplicatedError",
    "UnauthorizedError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError", "PhoneNumberBannedError",
    "PeerFloodError",                       # спам-блок: не ретраить, отдать наверх
    "ChatAdminRequiredError", "UserPrivacyRestrictedError",
    "ProxyIsolationError",                  # kill-switch: прямой выход запрещён
)


class FloodHandoff(Exception):
    """FloodWait пойман, клиент отключён — задачу нужно передать другому аккаунту."""

    def __init__(self, seconds: int, original: BaseException | None = None):
        super().__init__(f"FloodWait handoff: {seconds}s")
        self.seconds = int(seconds or 0)
        self.original = original


def classify_telethon_error(exc: BaseException) -> str:
    """'flood' | 'transient' | 'critical'. Неизвестное → 'critical' (безопасно:
    не ретраим вслепую, пробрасываем наверх)."""
    name = type(exc).__name__
    # FloodWait: у telethon это FloodWaitError с .seconds; ловим и по атрибуту.
    if "FloodWait" in name or (name.startswith("Flood") and hasattr(exc, "seconds")):
        return "flood"
    if name in _CRITICAL_NAMES:
        return "critical"
    if name in _TRANSIENT_NAMES or isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "critical"


def _backoff(attempt: int, base: float) -> float:
    """Экспоненциальный бэкофф с джиттером (±30%), ограниченный сверху."""
    raw = base * (2 ** attempt)
    jitter = raw * random.uniform(-0.3, 0.3)
    return max(0.3, min(raw + jitter, 30.0))


async def guarded_call(
    client: Any,
    factory: Callable[[], Awaitable[Any]],
    *,
    retries: int = 2,
    base_delay: float = 1.5,
    action: str = "tg",
) -> Any:
    """Выполнить сетевой вызов Telethon под предохранителем.

    `factory` — фабрика awaitable (пересоздаётся на каждую попытку, т.к. корутину
    нельзя await-ить дважды). `client` нужен, чтобы при FloodWait безопасно
    отключиться перед handoff.

    Бросает FloodHandoff при FloodWait; транзиентные ретраит; критичные пробрасывает.
    """
    attempt = 0
    while True:
        try:
            return await factory()
        except FloodHandoff:
            raise
        except BaseException as exc:  # noqa: BLE001 — классифицируем и решаем осознанно
            kind = classify_telethon_error(exc)
            if kind == "flood":
                secs = int(getattr(exc, "seconds", 0) or 0)
                try:
                    await client.disconnect()
                except Exception:
                    log.debug("guarded_call[%s]: disconnect после FloodWait не удался", action)
                log.warning("guarded_call[%s]: FloodWait %ds → handoff (клиент отключён)", action, secs)
                raise FloodHandoff(secs, original=exc) from exc
            if kind == "transient" and attempt < retries:
                delay = _backoff(attempt, base_delay)
                log.info("guarded_call[%s]: транзиентная ошибка %s → ретрай %d/%d через %.1fs",
                         action, type(exc).__name__, attempt + 1, retries, delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            # critical или исчерпан лимит ретраев — пробрасываем наверх
            raise
