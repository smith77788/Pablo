"""Реальный драйвер транспорта WB Chat — заглушка до поставки протокола.

У мессенджера WB Chat на сегодня нет публичного API/SDK и открытого клиента
протокола (проверено; см. docs/WB_CHAT.md). Пока протокол не поставлен, каждый
метод поднимает WBProtocolUnavailable с внятным указанием оператору — это НЕ
«тихо ничего не делает»: система массовых действий не должна притворяться
работающей на несуществующем транспорте.

Когда появится основа (официальный API Wildberries ИЛИ реверс-клиент), реализация
живёт ТОЛЬКО здесь: весь аккаунтный слой уже написан против WBChatTransport и не
меняется. Точки, которые предстоит закрыть, помечены TODO(protocol).
"""

from __future__ import annotations

from services.wb_chat.transport import (
    LoginChallenge,
    WBChatTransport,
    WBMessage,
    WBPeer,
    WBProtocolUnavailable,
    WBSession,
    WBUser,
)

_MSG = (
    "Протокол WB Chat не поставлен: у мессенджера нет публичного API/клиента. "
    "Реализуйте RealWBChatTransport (services/wb_chat/drivers/real.py) под "
    "официальный API WB или реверс-клиент, либо работайте с WB_CHAT_DRIVER=mock."
)


class RealWBChatTransport(WBChatTransport):
    def __init__(
        self,
        *,
        session: str | None = None,
        proxy: str | None = None,
        device: dict | None = None,
    ) -> None:
        # Параметры принимаем (единый вид с мок-драйвером и фабрикой), но до
        # реализации протокола ничего с ними не делаем.
        self._session = session
        self._proxy = proxy
        self._device = device or {}

    # TODO(protocol): установить соединение с транспортом WB Chat (через proxy).
    async def connect(self) -> None:
        raise WBProtocolUnavailable(_MSG)

    async def disconnect(self) -> None:
        # Безопасно как no-op: закрывать нечего, пока connect не реализован.
        return None

    # TODO(protocol): проверить действительность загруженной сессии.
    async def is_authorized(self) -> bool:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): вернуть профиль текущего аккаунта.
    async def get_me(self) -> WBUser | None:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): сериализовать текущую сессию в строку для хранения.
    def export_session(self) -> str:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): запросить код подтверждения на номер (WB ID).
    async def start_login(self, phone: str) -> LoginChallenge:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): завершить вход кодом/паролем, вернуть сессию.
    async def complete_login(
        self, challenge: LoginChallenge, code: str, password: str | None = None
    ) -> WBSession:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): разрешить ссылку/номер/username в адресата.
    async def resolve(self, ref: str) -> WBPeer:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): отправить текстовое сообщение адресату.
    async def send_message(self, peer: WBPeer | str, text: str) -> WBMessage:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): вступить в группу/канал.
    async def join(self, peer: WBPeer | str) -> None:
        raise WBProtocolUnavailable(_MSG)

    # TODO(protocol): выйти из группы/канала.
    async def leave(self, peer: WBPeer | str) -> None:
        raise WBProtocolUnavailable(_MSG)
