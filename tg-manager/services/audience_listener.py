"""Пассивный слушатель Update-потока флота tg_accounts (push, не poll).

Первопричина: MTProto — не request/response, сервер САМ пушит Updates в
подключённую сессию (так в любом официальном клиенте доставляются сообщения,
статусы «в сети», изменения профиля). Весь остальной код в проекте узнаёт о
происходящем через periodic connect→запрос→disconnect (см. activity_engine.py,
account_warmer.py и др.) — poll, а не push. Business Vault (intent_sensor) уже
событийный, но только для аккаунтов, подключённых через Telegram Business API
(bot/handlers/business_vault.py, @router.business_message()) — обычный флот
tg_accounts под управлением Telethon такой возможности не имел.

Слушать входящие сообщения в чатах, где аккаунт УЖЕ состоит — не эксплойт, а
нормальное поведение клиента: сервер и так обязан доставить это сообщение
подключённой сессии. Прямая польза: меньше активных запросов → меньше
флуд-сигналов → меньше риска бана, плюс мгновенная (а не отложенная до
следующего poll-цикла) реакция intent_sensor на входящие.

── Opt-in, не для всего флота ──────────────────────────────────────────────
Слушатель держит TCP-соединение аккаунта открытым ЧАСАМИ. Единственная сессия
Telegram не терпит двух одновременных подключений (AUTH_KEY_DUPLICATED,
безвозвратная смерть ключа — см. op_worker.py, account_warmer.py). Поэтому:
  • слушаем только аккаунты с явным tg_accounts.listener_enabled=TRUE —
    оператор осознанно выделяет их под мониторинг, а не под обычные операции;
  • захват — через ЕДИНЫЙ арбитр op_worker.try_claim_account/release_accounts
    (тот же, что использует ghost_engine.py) — аккаунт, занятый операцией,
    слушатель не тронет, и наоборот;
  • продление аренды — БЕСПЛАТНО: op_worker.renew_leases() уже вызывается в
    главном цикле воркера каждый опрос и продлевает ВСЕ аккаунты, арендованные
    этим процессом (см. op_worker.py:1422) — отдельный heartbeat не нужен.

In-memory состояние (`_listening`) не переживает рестарт и не шарится между
воркерами — при перезапуске процесса подключения переустанавливаются со
следующего тика, а зависшие in_operation-флаги освобождает
op_worker.reset_stale_in_operation() при старте (lease истекает сам).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

_SCAN_INTERVAL = 60  # секунд между проверками listener_enabled

# acc_id -> Telethon TelegramClient. Process-local (см. докстринг модуля).
_listening: dict[int, Any] = {}


def _peer_from_sender(sender: Any) -> dict[str, Any]:
    """Тот же формат dict, что services.vault_service.peer_of() строит из
    aiogram Message — intent_sensor.scan_incoming() ждёт именно его, откуда бы
    сообщение ни пришло (Business API или обычный Telethon-аккаунт)."""
    name = " ".join(
        x for x in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if x
    ) or None
    return {
        "peer_user_id": getattr(sender, "id", None),
        "peer_name": name,
        "peer_username": getattr(sender, "username", None),
    }


async def _on_incoming_message(pool, bot, owner_id: int, event) -> None:
    """Обработчик events.NewMessage(incoming=True) на слушающем клиенте."""
    from services.logger import log_exc_swallow

    try:
        text = getattr(event.message, "message", None)
        if not text:
            return
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        peer = _peer_from_sender(sender) if sender else {
            "peer_user_id": getattr(event, "sender_id", None),
            "peer_name": None, "peer_username": None,
        }
        from services import intent_sensor

        await intent_sensor.scan_incoming(pool, bot, owner_id, peer, text)
    except Exception:
        log_exc_swallow(log, f"audience_listener: обработка входящего owner={owner_id} упала")


async def _start_one(pool, bot, acc_id: int) -> bool:
    """Захватить аккаунт и открыть слушающее соединение. True — подключено."""
    from services.logger import log_exc_swallow
    from services import op_worker as _opw

    if not await _opw.try_claim_account(acc_id):
        return False

    claimed = True
    try:
        from database import db
        from services.account_manager import _make_client

        acc = await db.get_account_for_telethon(pool, acc_id)
        if not acc or not acc.get("session_str"):
            raise RuntimeError("нет сессии")

        client = _make_client(acc["session_str"], acc)
        await asyncio.wait_for(client.connect(), timeout=15)
        if not await client.is_user_authorized():
            raise RuntimeError("сессия не авторизована")

        from telethon import events

        owner_id = int(acc["owner_id"])
        client.add_event_handler(
            lambda e, _p=pool, _b=bot, _o=owner_id: _on_incoming_message(_p, _b, _o, e),
            events.NewMessage(incoming=True),
        )
        _listening[acc_id] = client
        log.info("audience_listener: acc=%d подключён (owner=%d)", acc_id, owner_id)
        return True
    except Exception as e:
        log_exc_swallow(log, f"audience_listener: не удалось подключить acc={acc_id}: {e}")
        if claimed:
            try:
                await _opw.release_accounts([acc_id])
            except Exception:
                pass
        return False


async def _stop_one(acc_id: int) -> None:
    """Отключить и освободить аккаунт обратно арбитру."""
    from services.logger import log_exc_swallow

    client = _listening.pop(acc_id, None)
    if client is not None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:
            log_exc_swallow(log, f"audience_listener: disconnect acc={acc_id} упал")
    try:
        from services import op_worker as _opw

        await _opw.release_accounts([acc_id])
    except Exception:
        log_exc_swallow(log, f"audience_listener: release acc={acc_id} упал")
    log.info("audience_listener: acc=%d отключён", acc_id)


async def desired_accounts(pool) -> set[int]:
    """Кто СЕЙЧАС должен слушаться: listener_enabled, живая сессия, не забанен.

    Чистая от побочных эффектов выборка — тестируется отдельно от подключения."""
    rows = await pool.fetch(
        """SELECT id FROM tg_accounts
           WHERE listener_enabled = TRUE AND is_active = TRUE
             AND session_str IS NOT NULL AND session_str <> ''
             AND COALESCE(acc_status, 'active') = 'active'"""
    )
    return {int(r["id"]) for r in (rows or [])}


async def tick(pool, bot) -> None:
    """Один проход: остановить лишних, подключить недостающих."""
    from services.logger import log_exc_swallow

    try:
        wanted = await desired_accounts(pool)
    except Exception:
        log_exc_swallow(log, "audience_listener: выборка listener_enabled упала")
        return

    for acc_id in [a for a in _listening if a not in wanted]:
        await _stop_one(acc_id)

    for acc_id in wanted:
        if acc_id not in _listening:
            await _start_one(pool, bot, acc_id)


async def run(pool, bot) -> None:
    """Фоновый цикл — регистрируется из main.py рядом с ghost_engine/immunity_engine."""
    from services.logger import log_exc_swallow

    log.info("audience_listener: старт (интервал=%ds)", _SCAN_INTERVAL)
    while True:
        try:
            await tick(pool, bot)
        except Exception:
            log_exc_swallow(log, "audience_listener: цикл упал (продолжаем)")
        await asyncio.sleep(_SCAN_INTERVAL)
