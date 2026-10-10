"""Мок-драйвер транспорта WB Chat — в памяти, детерминированный.

Назначение: разрабатывать и тестировать ВЕСЬ аккаунтный слой (вход, сессии,
ротацию, очередь операций, движок массовых DM) без реального протокола, которого
пока нет. Ничего не шлёт наружу: имитирует успешные ответы и ведёт учёт
отправленного, чтобы тесты могли это проверить.

Поведение приближено к реальному ради честности верхних слоёв:
  • start_login «отправляет» код (по умолчанию "0000"); неверный код → WBAuthError;
  • можно смоделировать flood и невалидного адресата через префиксы ссылок
    ("flood:*" → WBFloodWait, "invalid:*" → WBPeerInvalid) — так тестируются
    ретраи и обработка ошибок в op_worker/движке;
  • export_session отдаёт строку, по которой поднимается тот же аккаунт.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from services.wb_chat.transport import (
    LoginChallenge,
    WBAuthError,
    WBChatTransport,
    WBFloodWait,
    WBMessage,
    WBPeer,
    WBPeerInvalid,
    WBSession,
    WBUser,
)

# Общий на процесс «эфир»: учёт отправленного всеми мок-транспортами.
# Тесты читают его, чтобы проверить массовые операции. (chat_id → [тексты])
SENT_OUTBOX: dict[str, list[str]] = {}

# Код подтверждения, который мок принимает во complete_login.
DEFAULT_LOGIN_CODE = "0000"


def reset_mock_state() -> None:
    """Сбросить общий эфир (вызывать между тестами)."""
    SENT_OUTBOX.clear()


class MockWBChatTransport(WBChatTransport):
    def __init__(
        self,
        *,
        session: str | None = None,
        proxy: str | None = None,
        device: dict | None = None,
    ) -> None:
        self._proxy = proxy
        self._device = device or {}
        self._connected = False
        self._user: WBUser | None = None
        # Восстановление из экспортированной сессии.
        if session:
            self._load_session(session)

    # — сессия —
    def _load_session(self, session: str) -> None:
        try:
            obj = json.loads(session)
            self._user = WBUser(
                user_id=str(obj.get("user_id", "")),
                phone=str(obj.get("phone", "")),
                wb_id=str(obj.get("wb_id", "")),
                name=str(obj.get("name", "")),
            )
        except Exception:  # noqa: BLE001 — битая сессия → считаем неавторизованным
            self._user = None

    def export_session(self) -> str:
        u = self._user
        return json.dumps(
            {
                "user_id": u.user_id if u else "",
                "phone": u.phone if u else "",
                "wb_id": u.wb_id if u else "",
                "name": u.name if u else "",
            }
        )

    # — жизненный цикл —
    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_authorized(self) -> bool:
        return self._user is not None

    async def get_me(self) -> WBUser | None:
        return self._user

    # — вход —
    async def start_login(self, phone: str) -> LoginChallenge:
        phone = (phone or "").strip()
        if not phone:
            raise WBAuthError("пустой номер телефона")
        # «Отправили» код; state несёт ожидаемый код (в реале — challenge WB ID).
        return LoginChallenge(phone=phone, state={"expected_code": DEFAULT_LOGIN_CODE})

    async def complete_login(
        self, challenge: LoginChallenge, code: str, password: str | None = None
    ) -> WBSession:
        expected = challenge.state.get("expected_code", DEFAULT_LOGIN_CODE)
        if (code or "").strip() != expected:
            raise WBAuthError("неверный код подтверждения")
        # Детерминированный user_id из номера — стабильно между вызовами.
        uid = "u" + str(abs(hash(challenge.phone)) % 10_000_000)
        self._user = WBUser(
            user_id=uid,
            phone=challenge.phone,
            wb_id="wbid-" + uid,
            name=f"Account {challenge.phone[-4:]}",
        )
        return WBSession(data=self.export_session(), user=self._user)

    # — операции —
    async def resolve(self, ref: str) -> WBPeer:
        ref = (ref or "").strip()
        if not ref or ref.startswith("invalid:"):
            raise WBPeerInvalid(f"адресат не найден: {ref!r}")
        kind = "channel" if ref.startswith("@") else "user"
        return WBPeer(peer_id=ref.lstrip("@"), kind=kind, title=ref)

    async def send_message(self, peer: WBPeer | str, text: str) -> WBMessage:
        self._require_auth()
        p = await self._as_peer(peer)
        SENT_OUTBOX.setdefault(p.peer_id, []).append(text)
        return WBMessage(message_id=uuid.uuid4().hex, peer_id=p.peer_id, text=text)

    async def join(self, peer: WBPeer | str) -> None:
        self._require_auth()
        await self._as_peer(peer)  # валидация адресата (может кинуть invalid/flood)

    async def leave(self, peer: WBPeer | str) -> None:
        self._require_auth()
        await self._as_peer(peer)

    # — вспомогательное —
    def _require_auth(self) -> None:
        if self._user is None:
            raise WBAuthError("операция требует авторизованной сессии")

    async def _as_peer(self, peer: WBPeer | str) -> WBPeer:
        if isinstance(peer, WBPeer):
            return peer
        ref = str(peer).strip()
        # Моделирование ошибок протокола для проверки ретраев верхних слоёв.
        if ref.startswith("flood:"):
            raise WBFloodWait(3)
        return await self.resolve(ref)
