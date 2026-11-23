"""Настройки канала и группы владельца — то, что в Telegram лежит в «Изменить».

ЗАЧЕМ. В продукте можно было поменять название, описание, username и аватар,
но ни одной настройки самого канала/чата: тип (публичный/частный), подпись
авторов, запрет копирования, реакции, автоудаление, заявки на вступление,
обсуждение; у групп — разрешения участников, медленный режим, темы, история
для новичков. Владелец уходил за этим в Telegram, по одному каналу.

КАК. Каналы в каталоге (`managed_channels`) ведут АККАУНТЫ флота, а не бот,
поэтому настройки читаются и меняются через аккаунт-владельца (Telethon). Это
шире, чем умеет Bot API: боту, например, недоступны подпись авторов, реакции,
автоудаление и медленный режим.

Логика проверки значения и сборки снимка — ЧИСТАЯ (тестируется без Telegram);
сетевые функции тонкие: найти канал → один запрос → прочитать снимок заново.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Варианты автоудаления — те же, что предлагает Telegram.
TTL_CHOICES = (0, 86400, 604800, 2592000)
# Медленный режим — допустимые Telegram значения (секунды).
SLOWMODE_CHOICES = (0, 10, 30, 60, 300, 900, 3600)
REACTIONS_CHOICES = ("all", "none")

# Разрешения участников группы: ключ экрана → права ChatBannedRights.
# В Telegram флаг права означает ЗАПРЕТ; на экране — «разрешено».
PERM_RIGHTS: dict[str, tuple[str, ...]] = {
    "send_plain": ("send_plain",),
    "send_media": ("send_media", "send_photos", "send_videos", "send_roundvideos",
                   "send_audios", "send_voices", "send_docs"),
    "send_stickers": ("send_stickers", "send_gifs", "send_games", "send_inline"),
    "embed_links": ("embed_links",),
    "send_polls": ("send_polls",),
    "send_reactions": ("send_reactions",),
    "invite_users": ("invite_users",),
    "pin_messages": ("pin_messages",),
    "change_info": ("change_info",),
    "manage_topics": ("manage_topics",),
}
PERM_LABELS = {
    "send_plain": "Отправка сообщений",
    "send_media": "Фото, видео, файлы",
    "send_stickers": "Стикеры и GIF",
    "embed_links": "Ссылки",
    "send_polls": "Опросы",
    "send_reactions": "Реакции",
    "invite_users": "Добавление участников",
    "pin_messages": "Закрепление сообщений",
    "change_info": "Изменение профиля группы",
    "manage_topics": "Создание тем",
}

# Ключ настройки → к чему применим: 'any', 'channel' (только канал), 'group'.
SETTINGS: dict[str, str] = {
    "username": "any",          # '' — частный, иначе публичный с этим адресом
    "noforwards": "any",        # запрет копирования/пересылки
    "reactions": "any",         # all | none
    "ttl": "any",               # автоудаление, сек
    "join_request": "any",      # вступление только по заявке
    "signatures": "channel",    # подписывать сообщения
    "discussion": "channel",    # id группы обсуждения, 0 — отвязать
    "slowmode": "group",
    "forum": "group",           # темы
    "hidden_prehistory": "group",  # скрывать историю от новичков
    "join_to_send": "group",    # писать могут только вступившие
    "permissions": "group",     # {ключ PERM_RIGHTS: разрешено?}
}

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
        return value.lower() in ("true", "1")
    return None


def validate(key: str, value: Any, *, is_group: bool) -> tuple[Any, str]:
    """Проверить и нормализовать значение. (значение, '') или (None, ошибка). ЧИСТАЯ."""
    scope = SETTINGS.get(key)
    if scope is None:
        return None, "Неизвестная настройка"
    if scope == "channel" and is_group:
        return None, "Эта настройка есть только у каналов"
    if scope == "group" and not is_group:
        return None, "Эта настройка есть только у групп"
    if key == "username":
        u = str(value or "").strip().lstrip("@")
        u = re.sub(r"^(https?://)?t\.me/", "", u)
        if u and not _USERNAME_RE.match(u):
            return None, ("Адрес: 5–32 символа, латиница, цифры и «_», "
                          "начинается с буквы")
        return u, ""
    if key == "reactions":
        v = str(value or "").strip().lower()
        return (v, "") if v in REACTIONS_CHOICES else (None, "Выберите: все или никакие")
    if key in ("ttl", "slowmode"):
        choices = TTL_CHOICES if key == "ttl" else SLOWMODE_CHOICES
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, "Неверное значение"
        return (v, "") if v in choices else (None, "Такого значения Telegram не допускает")
    if key == "discussion":
        try:
            v = int(value or 0)
        except (TypeError, ValueError):
            return None, "Неверная группа"
        return v, ""
    if key == "permissions":
        if not isinstance(value, dict) or not value:
            return None, "Не переданы разрешения"
        out: dict[str, bool] = {}
        for k, v in value.items():
            b = _as_bool(v)
            if k not in PERM_RIGHTS or b is None:
                return None, "Неизвестное разрешение"
            out[k] = b
        return out, ""
    b = _as_bool(value)
    if b is None:
        return None, "Ожидается «вкл» или «выкл»"
    return b, ""


def permissions_from_rights(rights: Any) -> dict[str, bool]:
    """ChatBannedRights → {ключ: разрешено?}. ЧИСТАЯ (только getattr)."""
    out = {}
    for key, flags in PERM_RIGHTS.items():
        out[key] = not any(bool(getattr(rights, f, False)) for f in flags) if rights else True
    return out


def rights_kwargs(current: dict[str, bool], change: dict[str, bool]) -> dict[str, bool]:
    """Права для EditChatDefaultBannedRights: текущие + изменения. ЧИСТАЯ.

    Отдаём ВЕСЬ набор: запрос заменяет права целиком, и всё, что не передано,
    Telegram считает разрешённым — правка одного тумблера сняла бы остальные.
    """
    merged = dict(current)
    merged.update(change)
    kw: dict[str, bool] = {}
    for key, flags in PERM_RIGHTS.items():
        banned = not merged.get(key, True)
        for f in flags:
            kw[f] = banned
    # Запрет обычных сообщений у Telegram ещё и общий флаг send_messages.
    kw["send_messages"] = kw.get("send_plain", False)
    return kw


def snapshot(full_chat: Any, chat: Any) -> dict:
    """Снимок настроек из GetFullChannel. ЧИСТАЯ (только getattr)."""
    is_group = bool(getattr(chat, "megagroup", False))
    reactions = type(getattr(full_chat, "available_reactions", None)).__name__
    snap = {
        "is_group": is_group,
        "title": getattr(chat, "title", "") or "",
        "about": getattr(full_chat, "about", "") or "",
        "username": getattr(chat, "username", "") or "",
        "noforwards": bool(getattr(chat, "noforwards", False)),
        "reactions": {"ChatReactionsAll": "all", "ChatReactionsNone": "none",
                      "ChatReactionsSome": "some"}.get(reactions, "all"),
        "ttl": int(getattr(full_chat, "ttl_period", 0) or 0),
        "join_request": bool(getattr(chat, "join_request", False)),
        "members": int(getattr(full_chat, "participants_count", 0) or 0),
        "admins": int(getattr(full_chat, "admins_count", 0) or 0),
        "banned": int(getattr(full_chat, "kicked_count", 0) or 0),
        "requests_pending": int(getattr(full_chat, "requests_pending", 0) or 0),
        "linked_chat_id": int(getattr(full_chat, "linked_chat_id", 0) or 0),
        "is_creator": bool(getattr(chat, "creator", False)),
    }
    if is_group:
        snap.update({
            "slowmode": int(getattr(full_chat, "slowmode_seconds", 0) or 0),
            "forum": bool(getattr(chat, "forum", False)),
            "hidden_prehistory": bool(getattr(full_chat, "hidden_prehistory", False)),
            "join_to_send": bool(getattr(chat, "join_to_send", False)),
            "permissions": permissions_from_rights(
                getattr(chat, "default_banned_rights", None)),
        })
    else:
        snap["signatures"] = bool(getattr(chat, "signatures", False))
    return snap


def error_ru(exc: BaseException) -> str:
    """Отказ Telegram на смену настройки — по-русски."""
    name = type(exc).__name__
    text = str(exc).upper()
    if isinstance(exc, asyncio.TimeoutError):
        return "Telegram не ответил вовремя (проверьте прокси аккаунта)"
    if name == "FloodWaitError":
        return f"Флуд-лимит Telegram, подождите {getattr(exc, 'seconds', 0)} сек."
    if "USERNAME_OCCUPIED" in text or name == "UsernameOccupiedError":
        return "Этот адрес уже занят"
    if "USERNAME_INVALID" in text:
        return "Недопустимый адрес"
    if "CHANNELS_ADMIN_PUBLIC_TOO_MUCH" in text:
        return "У аккаунта слишком много публичных каналов — освободите адрес в другом"
    if "CHAT_NOT_MODIFIED" in text or name == "ChatNotModifiedError":
        return ""
    if "BROADCAST_ID_INVALID" in text or "MEGAGROUP_ID_INVALID" in text:
        return "Эту группу нельзя сделать обсуждением (нужна супергруппа без своего канала)"
    if "LINK_NOT_MODIFIED" in text:
        return ""
    if "BOOSTS" in text or "PREMIUM" in text:
        return "Telegram требует уровень буста канала для этой настройки"
    if name in ("ChatAdminRequiredError", "RightForbiddenError") or "ADMIN" in text:
        return "У аккаунта нет нужных прав администратора в этом канале"
    if name in ("ChannelPrivateError", "ChannelInvalidError", "ValueError"):
        return "Аккаунт не видит канал: он удалён или доступ потерян"
    return "Telegram отклонил изменение: " + (str(exc) or name)[:100]


# ── Сеть ─────────────────────────────────────────────────────────────────

async def _open(session_string: str, acc: dict | None, channel_id: int,
                access_hash: int, username: str):
    from services import account_manager as am

    client = am._make_client(session_string, acc)
    await asyncio.wait_for(client.connect(), timeout=am._CONNECT_TIMEOUT)
    try:
        peer = await am._resolve_managed_channel(client, channel_id, access_hash, username)
    except Exception:
        await am.safe_disconnect(client)
        raise
    return client, peer


async def _read(client, peer, channel_id: int) -> dict:
    from telethon.tl.functions.channels import GetFullChannelRequest

    from services import account_manager as am

    full = await asyncio.wait_for(client(GetFullChannelRequest(peer)), timeout=am._OP_TIMEOUT)
    chat = am._chat_from_full(full, channel_id)
    return snapshot(full.full_chat, chat)


async def read_settings(session_string: str, channel_id: int, *, acc: dict | None = None,
                        access_hash: int = 0, username: str = "") -> dict:
    """Текущие настройки. Бросает исключение — вызывающий переводит error_ru."""
    from services import account_manager as am

    client, peer = await _open(session_string, acc, channel_id, access_hash, username)
    try:
        return await _read(client, peer, channel_id)
    finally:
        await am.safe_disconnect(client)


async def apply_setting(session_string: str, channel_id: int, key: str, value: Any, *,
                        acc: dict | None = None, access_hash: int = 0, username: str = "",
                        group_ref: tuple[int, int] | None = None) -> dict:
    """Поменять одну настройку и вернуть снимок ПОСЛЕ (то, что реально стоит).

    `value` уже прошёл `validate`. `group_ref` — (id, access_hash) группы для
    обсуждения. Ошибку Telegram бросает наверх.
    """
    from telethon.tl import functions as F
    from telethon.tl import types as T

    from services import account_manager as am

    client, peer = await _open(session_string, acc, channel_id, access_hash, username)
    try:
        req = None
        if key == "username":
            req = F.channels.UpdateUsernameRequest(channel=peer, username=value)
        elif key == "noforwards":
            req = F.messages.ToggleNoForwardsRequest(peer=peer, enabled=value)
        elif key == "reactions":
            req = F.messages.SetChatAvailableReactionsRequest(
                peer=peer, available_reactions=(T.ChatReactionsAll(allow_custom=True)
                                                if value == "all" else T.ChatReactionsNone()))
        elif key == "ttl":
            req = F.messages.SetHistoryTTLRequest(peer=peer, period=value)
        elif key == "join_request":
            req = F.channels.ToggleJoinRequestRequest(channel=peer, enabled=value)
        elif key == "signatures":
            req = F.channels.ToggleSignaturesRequest(channel=peer, signatures_enabled=value)
        elif key == "slowmode":
            req = F.channels.ToggleSlowModeRequest(channel=peer, seconds=value)
        elif key == "forum":
            req = F.channels.ToggleForumRequest(channel=peer, enabled=value, tabs=False)
        elif key == "hidden_prehistory":
            req = F.channels.TogglePreHistoryHiddenRequest(channel=peer, enabled=value)
        elif key == "join_to_send":
            req = F.channels.ToggleJoinToSendRequest(channel=peer, enabled=value)
        elif key == "permissions":
            cur = (await _read(client, peer, channel_id)).get("permissions") or {}
            req = F.messages.EditChatDefaultBannedRightsRequest(
                peer=peer, banned_rights=T.ChatBannedRights(
                    until_date=None, **rights_kwargs(cur, value)))
        elif key == "discussion":
            if value:
                gid, ghash = group_ref or (value, 0)
                group = await am._resolve_channel_peer(client, gid, int(ghash or 0))
            else:
                group = T.InputChannelEmpty()
            req = F.channels.SetDiscussionGroupRequest(broadcast=peer, group=group)
        if req is None:
            raise ValueError("неизвестная настройка")
        try:
            await asyncio.wait_for(client(req), timeout=am._OP_TIMEOUT)
        except Exception as e:
            # «Ничего не изменилось» — это успех: значение уже такое.
            if error_ru(e):
                raise
        return await _read(client, peer, channel_id)
    finally:
        await am.safe_disconnect(client)
