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


async def _resolve_group_entity(client: Any, group_ref: str) -> Any:
    """Resolve a group reference to an entity usable by InviteToChannelRequest.

    Handles both public (@username / t.me/username) and private invite-link
    (t.me/+HASH, t.me/joinchat/HASH) references. The naive `client.get_entity()`
    on a raw invite-link string either mismatches "joinchat" as a bogus username
    or fails outright for `+HASH` links — Telethon can't resolve an invite hash
    to a peer without ImportChatInviteRequest/CheckChatInviteRequest.
    """
    from services.account_manager import normalize_telegram_join_ref

    ref_kind, ref_value = normalize_telegram_join_ref(group_ref)
    if ref_kind != "invite":
        return await asyncio.wait_for(
            client.get_entity(ref_value or group_ref), timeout=_ACTION_TIMEOUT
        )

    from telethon.tl.functions.messages import (
        ImportChatInviteRequest,
        CheckChatInviteRequest,
    )
    from telethon.errors import UserAlreadyParticipantError

    try:
        result = await asyncio.wait_for(
            client(ImportChatInviteRequest(hash=ref_value)), timeout=_ACTION_TIMEOUT
        )
        chats = getattr(result, "chats", None) or []
        if chats:
            return chats[0]
        raise ValueError("ImportChatInviteRequest returned no chat")
    except UserAlreadyParticipantError:
        # Account is already a member — peek instead of re-joining to get the entity.
        info = await asyncio.wait_for(
            client(CheckChatInviteRequest(hash=ref_value)), timeout=_ACTION_TIMEOUT
        )
        chat = getattr(info, "chat", None)
        if chat is None:
            raise
        return chat


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
        group = await _resolve_group_entity(client, group_ref)

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
        group = await _resolve_group_entity(client, group_ref)

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
    """Normalize a user-typed group reference (@name / t.me/name / numeric ID /
    private invite link) to a canonical form _resolve_group_entity() can consume.

    Delegates public-vs-invite classification to account_manager's
    normalize_telegram_join_ref — the previous hand-rolled regex here matched
    "t.me/joinchat/HASH" as if "joinchat" were a public username, and didn't
    match "t.me/+HASH" at all (the plus sign isn't in its character class),
    silently corrupting every private invite link passed to this feature.
    """
    text = text.strip()
    if re.match(r"^-?\d+$", text):
        return text  # numeric chat ID — not a join ref, pass through as-is

    from services.account_manager import format_telegram_join_ref_display

    formatted = format_telegram_join_ref_display(text)
    return formatted or text


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
