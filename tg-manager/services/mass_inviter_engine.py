"""Движок массового инвайтинга.

Поддерживает:
  - Добавление по user_id / @username
  - Добавление по номерам телефонов (import contact → invite → delete contact)

Обрабатывает:
  - UserPrivacyRestricted → пропустить
  - PeerFloodError → аккаунт перегрет, переключиться
  - UserNotMutualContact → только для закрытых групп, пропустить
  - FloodWaitError → пауза + flood_engine
  - UserAlreadyParticipant → считать как успех
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 15.0
_BATCH_SIZE = 5  # Telegram разрешает добавлять до 5 за раз без флуда


# ── Добавление пачки user_id/username ────────────────────────────────────────

async def invite_batch(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    user_refs: list[str | int],
) -> dict[str, Any]:
    """Добавить список пользователей (ID или @username) в группу.

    Возвращает:
      {"ok": int, "failed": int, "peer_flood": bool, "errors": list[str]}
    """
    from services.account_manager import _make_client
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.errors import (
        UserPrivacyRestrictedError,
        UserAlreadyParticipantError,
        PeerFloodError,
        UserNotMutualContactError,
        FloodWaitError,
        ChatWriteForbiddenError,
        ChannelPrivateError,
    )

    client = _make_client(session_string, _acc)
    ok, failed = 0, 0
    errors: list[str] = []
    peer_flood = False

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        group = await asyncio.wait_for(client.get_entity(group_ref), timeout=_ACTION_TIMEOUT)

        for ref in user_refs:
            try:
                user = await asyncio.wait_for(client.get_entity(ref), timeout=_ACTION_TIMEOUT)
                await asyncio.wait_for(
                    client(InviteToChannelRequest(channel=group, users=[user])),
                    timeout=_ACTION_TIMEOUT,
                )
                ok += 1
                await asyncio.sleep(random.uniform(2.0, 4.0))
            except UserAlreadyParticipantError:
                ok += 1  # уже в группе = успех
            except UserPrivacyRestrictedError:
                failed += 1
                errors.append(f"{ref}: privacy restricted")
            except UserNotMutualContactError:
                failed += 1
                errors.append(f"{ref}: not mutual contact")
            except PeerFloodError:
                peer_flood = True
                failed += 1
                errors.append(f"{ref}: peer flood — аккаунт ограничен")
                break  # аккаунт перегрет, дальше не пробуем
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
                failed += 1
                errors.append(f"{ref}: flood wait {e.seconds}s")
            except (ChatWriteForbiddenError, ChannelPrivateError) as e:
                failed += 1
                errors.append(f"group error: {e}")
                break  # нет прав/группа закрыта
            except Exception as e:
                failed += 1
                errors.append(f"{ref}: {str(e)[:80]}")

    except Exception as exc:
        log.warning("invite_batch connect/group error: %s", exc)
        errors.append(f"connect: {str(exc)[:100]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood, "errors": errors}


# ── Добавление по номерам телефонов ──────────────────────────────────────────

async def invite_by_phones(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    phones: list[str],
) -> dict[str, Any]:
    """Добавить список номеров телефонов в группу.

    Алгоритм: ImportContactsRequest → получить user_id → InviteToChannel → DeleteContacts.
    """
    from services.account_manager import _make_client
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
    from telethon.tl.types import InputPhoneContact
    from telethon.errors import (
        UserPrivacyRestrictedError,
        UserAlreadyParticipantError,
        PeerFloodError,
        FloodWaitError,
    )

    client = _make_client(session_string, _acc)
    ok, failed = 0, 0
    errors: list[str] = []
    peer_flood = False
    imported_users: list = []

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        group = await asyncio.wait_for(client.get_entity(group_ref), timeout=_ACTION_TIMEOUT)

        # Импортируем контакты
        contacts = [
            InputPhoneContact(client_id=i, phone=p, first_name="u", last_name="")
            for i, p in enumerate(phones)
        ]
        result = await asyncio.wait_for(
            client(ImportContactsRequest(contacts=contacts)),
            timeout=_ACTION_TIMEOUT,
        )
        imported_users = list(result.users)
        log.info("invite_by_phones: imported %d/%d users", len(imported_users), len(phones))

        for user in imported_users:
            if peer_flood:
                break
            try:
                await asyncio.wait_for(
                    client(InviteToChannelRequest(channel=group, users=[user])),
                    timeout=_ACTION_TIMEOUT,
                )
                ok += 1
                await asyncio.sleep(random.uniform(2.5, 5.0))
            except UserAlreadyParticipantError:
                ok += 1
            except UserPrivacyRestrictedError:
                failed += 1
            except PeerFloodError:
                peer_flood = True
                failed += 1
                errors.append("peer flood — аккаунт ограничен")
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 60))
                failed += 1
            except Exception as e:
                failed += 1
                errors.append(str(e)[:80])

        # Удаляем импортированные контакты
        if imported_users:
            try:
                await asyncio.wait_for(
                    client(DeleteContactsRequest(id=imported_users)),
                    timeout=_ACTION_TIMEOUT,
                )
            except Exception:
                pass

        # Пользователи из телефонов которых не нашли
        not_found = len(phones) - len(imported_users)
        if not_found:
            failed += not_found
            errors.append(f"{not_found} номеров не зарегистрированы в Telegram")

    except Exception as exc:
        log.warning("invite_by_phones error: %s", exc)
        errors.append(str(exc)[:100])
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood, "errors": errors}


# ── Утилиты ──────────────────────────────────────────────────────────────────

def parse_group_ref(text: str) -> str:
    text = text.strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]{3,})", text)
    if m:
        return f"@{m.group(1)}"
    if text.startswith("@"):
        return text
    if re.match(r"^-?\d+$", text):
        return text
    if re.match(r"^[A-Za-z0-9_]{3,}$", text):
        return f"@{text}"
    return text


def parse_user_refs(text: str) -> list[str]:
    """Парсинг строки с @username или ID через запятую/пробел/перенос."""
    refs: list[str] = []
    for token in re.split(r"[,;\s\n]+", text.strip()):
        token = token.strip().lstrip("@")
        if not token:
            continue
        if re.match(r"^\d+$", token):
            refs.append(token)
        elif re.match(r"^[A-Za-z0-9_]{3,}$", token):
            refs.append(f"@{token}")
    return list(dict.fromkeys(refs))[:500]


def parse_phones(text: str) -> list[str]:
    """Парсинг номеров телефонов: +79991234567 через любой разделитель."""
    phones: list[str] = []
    for token in re.split(r"[,;\s\n]+", text.strip()):
        token = re.sub(r"[^\d+]", "", token)
        if len(token) >= 10:
            if not token.startswith("+"):
                token = "+" + token
            phones.append(token)
    return list(dict.fromkeys(phones))[:500]
