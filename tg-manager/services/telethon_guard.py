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

Классификация — по ТОЧНОМУ имени класса, числовому коду RPCError и атрибутам
(без жёстких импортов telethon), поэтому модуль импортируется и тестируется без
сети и без telethon.

Почему код, а не только имена. В telethon 1.36 у ServerError (500) тридцать
подклассов, у FloodError (420) — пять, и перечислить их поимённо нельзя: список
растёт со схемой Telegram. Зато `RPCError.code` есть у каждого и стабилен —
по нему целый класс ошибок разбирается разом. Без этого 26 из 30 транзиентных
серверных сбоев (ChatGetFailedError, HistoryGetFailedError, RandomIdDuplicateError,
PersistentTimestampOutdatedError и прочие «повтори запрос») считались
критичными и не ретраились ни разу.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# Транзиентные (стоит ретраить) — по ТОЧНОМУ имени класса исключения. Это лишь
# быстрый путь и подстраховка для не-RPC ошибок; целые семейства разбирает код
# ниже (_TRANSIENT_CODES).
_TRANSIENT_NAMES = (
    "TimeoutError", "ConnectionError", "ConnectionResetError", "OSError",
    "ServerError", "RpcCallFailError", "RpcMcgetFailError", "TimedOutError",
    "DisconnectError", "ConnectionAbortedError",
)
# Критичные (ретрай вреден/бесполезен) — сессия/права/спам-блок. Проверяются
# ПЕРЕД кодом: имя здесь — осознанное решение и перекрывает общее правило.
_CRITICAL_NAMES = (
    "AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyDuplicatedError",
    "UnauthorizedError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError", "PhoneNumberBannedError",
    "PeerFloodError",                       # спам-блок: не ретраить, отдать наверх
    "ChatAdminRequiredError", "UserPrivacyRestrictedError",
    "ProxyIsolationError",                  # kill-switch: прямой выход запрещён
)

# Коды RPCError, которые стоит ретраить: 500 INTERNAL и 503 Timeout. Telegram
# отдаёт их и со знаком минус (в telethon так и написано: «Also witnessed as
# -500»), поэтому сравниваем по модулю.
_TRANSIENT_CODES = frozenset({500, 503})

# 420 — семейство FloodError целиком: FloodWaitError, SlowModeWaitError,
# TakeoutInitDelayError, FloodTestPhoneWaitError. Все несут .seconds и все
# означают одно: подождать столько-то. Раньше «flood» узнавали только по имени,
# начинающемуся с Flood, и ожидание слоу-мода при отправке в чат считалось
# фатальной ошибкой аккаунта.
_FLOOD_CODE = 420


class FloodHandoff(Exception):
    """FloodWait пойман, клиент отключён — задачу нужно передать другому аккаунту."""

    def __init__(self, seconds: int, original: BaseException | None = None):
        super().__init__(f"FloodWait handoff: {seconds}s")
        self.seconds = int(seconds or 0)
        self.original = original


def _rpc_code(exc: BaseException) -> int | None:
    """Числовой код RPCError по модулю, если он есть и осмыслен."""
    raw = getattr(exc, "code", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return abs(int(raw))
    except (TypeError, ValueError):
        return None


def classify_telethon_error(exc: BaseException) -> str:
    """'flood' | 'transient' | 'critical'. Неизвестное → 'critical' (безопасно:
    не ретраим вслепую, пробрасываем наверх)."""
    name = type(exc).__name__
    code = _rpc_code(exc)
    # Ожидание: у telethon это семейство FloodError (420) с .seconds. Ловим и по
    # имени (на случай подмены/обёртки), и по коду — так же попадают
    # SlowModeWait, TakeoutInitDelay и прочие «подожди N секунд».
    if "FloodWait" in name or (name.startswith("Flood") and hasattr(exc, "seconds")):
        return "flood"
    if code == _FLOOD_CODE and hasattr(exc, "seconds"):
        return "flood"
    # Осознанные решения по имени сильнее общего правила по коду.
    if name in _CRITICAL_NAMES:
        return "critical"
    if name in _TRANSIENT_NAMES or isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return "transient"
    if code in _TRANSIENT_CODES:
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
        except asyncio.CancelledError:
            # Отмена операции — не ошибка Telegram. Ниже ловится BaseException,
            # и без этой ветки отмена пошла бы в классификатор; стоит ему хоть
            # раз счесть её транзиентной — и мы уснём в уже отменённой задаче,
            # проглотив отмену.
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
