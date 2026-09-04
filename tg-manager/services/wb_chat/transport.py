"""Транспортный слой WB Chat — абстракция протокола (аналог Telethon).

У мессенджера WB Chat (chat.wb.ru, приложения из сторов) на сегодня НЕТ
публичного API/SDK и открытого клиента протокола — в отличие от Telegram, чью
автоматизацию Infragram строит на Telethon (зрелый клиент MTProto). Поэтому весь
аккаунтный слой (аккаунты, сессии, прокси, вход, очередь операций, движки
массовых действий) строится поверх ЭТОГО интерфейса — единственного шва, куда
подключается реальный протокол, когда он появится (официальный API или
реверс-клиент).

`WBChatTransport` — то, чем для Telegram является `TelegramClient`: одна сессия
одного аккаунта. Драйверы:
    • MockWBChatTransport  — в памяти, для dev/тестов (drivers/mock.py);
    • RealWBChatTransport  — заглушка, поднимающая WBProtocolUnavailable, пока
      протокол не поставлен (drivers/real.py).
Драйвер выбирается фабрикой `build_transport` по config.WB_CHAT_DRIVER — как
`account_manager._make_client` выбирает параметры Telethon-клиента.

Слои выше транспорта НЕ знают, какой драйвер активен: они работают только через
этот интерфейс и его модели/ошибки. Это и есть «построить архитектуру сейчас».
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


# ── Ошибки (аналог иерархии ошибок Telethon) ─────────────────────────────────
class WBChatError(Exception):
    """Базовая ошибка транспорта WB Chat."""


class WBProtocolUnavailable(WBChatError):
    """Реальный протокол WB Chat ещё не поставлен (нет официального API/клиента).

    Поднимается реальным драйвером-заглушкой. Верхние слои НЕ должны её ловить,
    чтобы «случайно работать» на несуществующем протоколе, — она сигнализирует
    оператору: подключите драйвер (config.WB_CHAT_DRIVER) или дождитесь API."""


class WBAuthError(WBChatError):
    """Ошибка входа: неверный код/пароль, истёкший челлендж, забаненный номер."""


class WBFloodWait(WBChatError):
    """WB попросил подождать N секунд (аналог FloodWaitError Telethon)."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"WB flood wait: {seconds}s")
        self.seconds = int(seconds)


class WBPeerInvalid(WBChatError):
    """Адресат не найден/недоступен (нельзя написать/вступить)."""


# ── Модели (минимум, нужный аккаунтному слою и движку массовых DM) ────────────
@dataclass(frozen=True)
class WBUser:
    """Профиль своего аккаунта (результат get_me) или собеседника."""

    user_id: str
    phone: str = ""
    wb_id: str = ""
    name: str = ""
    username: str = ""


@dataclass(frozen=True)
class WBPeer:
    """Разрешённый адресат: пользователь, группа или канал WB Chat."""

    peer_id: str
    kind: str = "user"          # 'user' | 'group' | 'channel'
    title: str = ""


@dataclass(frozen=True)
class WBMessage:
    """Результат отправки сообщения."""

    message_id: str
    peer_id: str
    text: str


@dataclass(frozen=True)
class WBSession:
    """Строка сессии аккаунта (то, что Telethon зовёт StringSession).

    `data` — непрозрачная для верхних слоёв строка драйвера; хранится в БД
    зашифрованной (token_vault). `user` — кто вошёл (для привязки к аккаунту)."""

    data: str
    user: WBUser | None = None


@dataclass
class LoginChallenge:
    """Промежуточное состояние входа между «запросили код» и «ввели код».

    Аналог phone_code_hash в Telethon. `state` — непрозрачные данные драйвера,
    которые нужно вернуть в complete_login (например, идентификатор challenge WB
    ID и временный ключ)."""

    phone: str
    state: dict = field(default_factory=dict)
    needs_password: bool = False   # у WB ID возможен доп. пароль/2FA


# ── Интерфейс транспорта ─────────────────────────────────────────────────────
class WBChatTransport(abc.ABC):
    """Один аккаунт WB Chat. Реализуется драйвером конкретного протокола.

    Контракт для авторов драйверов:
      • методы асинхронные; сетевые сбои → WBChatError (или подкласс);
      • rate-limit → WBFloodWait(seconds) — верхний слой сам выждет и повторит;
      • export_session() отдаёт строку, по которой позже поднимается тот же
        аккаунт без повторного входа (её шифруем и кладём в БД);
      • методы идемпотентны по смыслу, где это возможно (join уже вступившего —
        не ошибка).
    """

    # — жизненный цикл —
    @abc.abstractmethod
    async def connect(self) -> None:
        """Установить соединение (или восстановить из загруженной сессии)."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Закрыть соединение (освободить сокет/прокси)."""

    @abc.abstractmethod
    async def is_authorized(self) -> bool:
        """Есть ли действующая авторизация у текущей сессии."""

    @abc.abstractmethod
    async def get_me(self) -> WBUser | None:
        """Профиль текущего аккаунта; None — если не авторизован."""

    @abc.abstractmethod
    def export_session(self) -> str:
        """Сериализовать текущую сессию в строку (для хранения в БД)."""

    # — вход по номеру (WB ID) —
    @abc.abstractmethod
    async def start_login(self, phone: str) -> LoginChallenge:
        """Запросить код подтверждения на номер. Вернуть челлендж для завершения."""

    @abc.abstractmethod
    async def complete_login(
        self, challenge: LoginChallenge, code: str, password: str | None = None
    ) -> WBSession:
        """Завершить вход кодом (и паролем при needs_password). Вернуть сессию."""

    # — операции —
    @abc.abstractmethod
    async def resolve(self, ref: str) -> WBPeer:
        """Разрешить ссылку/номер/username в адресата. WBPeerInvalid — если нет."""

    @abc.abstractmethod
    async def send_message(self, peer: WBPeer | str, text: str) -> WBMessage:
        """Отправить текст адресату (WBPeer или сырая ссылка — тогда resolve сам)."""

    @abc.abstractmethod
    async def join(self, peer: WBPeer | str) -> None:
        """Вступить в группу/канал. Повторное вступление — не ошибка."""

    @abc.abstractmethod
    async def leave(self, peer: WBPeer | str) -> None:
        """Выйти из группы/канала."""

    # — контекстный менеджер: гарантированный disconnect —
    async def __aenter__(self) -> "WBChatTransport":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()


# ── Фабрика драйверов (аналог account_manager._make_client) ───────────────────
def build_transport(
    *,
    session: str | None = None,
    proxy: str | None = None,
    device: dict | None = None,
    driver: str | None = None,
) -> WBChatTransport:
    """Создать транспорт нужного драйвера.

    driver: 'mock' | 'real'. По умолчанию — из config.WB_CHAT_DRIVER. Мок изолирован
    и безопасен; 'real' поднимет WBProtocolUnavailable до поставки протокола.
    session — экспортированная строка (расшифрованная) для восстановления входа.
    """
    name = (driver or _configured_driver()).strip().lower()
    if name == "mock":
        from services.wb_chat.drivers.mock import MockWBChatTransport
        return MockWBChatTransport(session=session, proxy=proxy, device=device)
    if name == "real":
        from services.wb_chat.drivers.real import RealWBChatTransport
        return RealWBChatTransport(session=session, proxy=proxy, device=device)
    raise WBChatError(f"Неизвестный WB_CHAT_DRIVER={name!r} (ожидалось 'mock' или 'real')")


def _configured_driver() -> str:
    try:
        import config
        return getattr(config, "WB_CHAT_DRIVER", "mock")
    except Exception:  # noqa: BLE001 — конфиг может быть недоступен в изоляции теста
        return "mock"
