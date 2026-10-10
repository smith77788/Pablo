"""Вход в WB Chat по номеру (WB ID) через транспорт — двухшаговый поток.

Аналог phone-login у Telegram (start → код → sign_in → сохранить сессию):

    token = await login.start(phone, proxy=…)      # «отправили код»
    account = await login.complete(pool, owner_id, token, code)   # ввели код

Между шагами живой транспорт держится в памяти процесса (как _pending в
account_manager) — челлендж WB ID привязан к конкретному соединению. По успеху
сессия экспортируется, шифруется и кладётся в wb_accounts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg

from services.wb_chat import accounts
from services.wb_chat.transport import (
    LoginChallenge,
    WBAuthError,
    WBChatTransport,
    build_transport,
)


@dataclass
class _Pending:
    transport: WBChatTransport
    challenge: LoginChallenge
    owner_id: int
    proxy: str | None
    device: dict | None


# token → незавершённый вход. Живёт только в памяти процесса (секунды-минуты).
_PENDING: dict[str, _Pending] = {}


async def start(
    phone: str,
    *,
    owner_id: int = 0,
    proxy: str | None = None,
    device: dict | None = None,
    driver: str | None = None,
) -> str:
    """Шаг 1: запросить код на номер. Вернуть login_token для шага 2."""
    transport = build_transport(session=None, proxy=proxy, device=device, driver=driver)
    await transport.connect()
    challenge = await transport.start_login(phone)
    token = uuid.uuid4().hex
    _PENDING[token] = _Pending(transport, challenge, owner_id, proxy, device)
    return token


async def complete(
    pool: asyncpg.Pool,
    login_token: str,
    code: str,
    *,
    password: str | None = None,
) -> dict:
    """Шаг 2: подтвердить код (и пароль при 2FA), сохранить аккаунт. Вернуть его."""
    pending = _PENDING.get(login_token)
    if pending is None:
        raise WBAuthError("сессия входа не найдена или истекла — начните заново")

    transport = pending.transport
    try:
        session = await transport.complete_login(pending.challenge, code, password)
        me = session.user or await transport.get_me()
        account = await accounts.upsert_account(
            pool,
            owner_id=pending.owner_id,
            phone=pending.challenge.phone,
            session=session.data,
            user_id=me.user_id if me else "",
            wb_id=me.wb_id if me else "",
            name=me.name if me else "",
            proxy=pending.proxy,
            device=pending.device,
            status="active",
        )
        return account
    finally:
        # Закрываем соединение входа и чистим память в любом исходе.
        try:
            await transport.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _PENDING.pop(login_token, None)


def pending_count() -> int:
    """Число незавершённых входов (для диагностики/тестов)."""
    return len(_PENDING)
