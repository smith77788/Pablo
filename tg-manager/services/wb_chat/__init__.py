"""WB Chat — аккаунтная автоматизация (по образцу Telegram-стека Infragram).

WB Chat — новый самостоятельный мессенджер Wildberries (приложения из сторов,
chat.wb.ru), аналог Telegram. Автоматизация Telegram в Infragram — это прежде
всего управление ПОЛЬЗОВАТЕЛЬСКИМИ аккаунтами через Telethon (сессии, прокси,
массовые действия). Этот пакет воспроизводит ту же архитектуру для WB Chat.

У WB Chat пока нет публичного API/клиента протокола, поэтому весь аккаунтный слой
построен поверх абстрактного транспорта (transport.WBChatTransport); реальный
протокол подключается одним драйвером (drivers/real.py), когда появится.

    Telegram (существующее)              WB Chat (этот пакет)
    ───────────────────────              ────────────────────
    TelegramClient / Telethon        ->  transport.WBChatTransport + drivers/
    account_manager._make_client     ->  transport.build_transport / session_manager
    phone-login FSM                  ->  login.py
    accounts + сессии/прокси/здоровье -> accounts.py + schema_v185 (wb_accounts)
    op_worker + очередь операций     ->  op_worker.py + wb_operations
    *_engine.py (масс-действия)      ->  engines/ (эталон: mass_dm.py)
    flood_engine (пейсинг/брейкер)   ->  pacing.py

Точка входа фонового воркера подключается в main.py через _resilient, как и
прочие сервисы. Транспорт по умолчанию — mock (config.WB_CHAT_DRIVER), безопасный
для dev/тестов; 'real' до поставки протокола поднимает WBProtocolUnavailable.
"""

from __future__ import annotations

from services.wb_chat.transport import (
    LoginChallenge,
    WBChatError,
    WBChatTransport,
    WBFloodWait,
    WBMessage,
    WBPeer,
    WBPeerInvalid,
    WBProtocolUnavailable,
    WBSession,
    WBUser,
    build_transport,
)

__all__ = [
    "WBChatTransport",
    "WBChatError",
    "WBProtocolUnavailable",
    "WBFloodWait",
    "WBPeerInvalid",
    "WBUser",
    "WBPeer",
    "WBMessage",
    "WBSession",
    "LoginChallenge",
    "build_transport",
]
