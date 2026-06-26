"""Движок накрутки: просмотры, реакции, просмотр сторис.

Каждая функция принимает session_string + device dict (_acc),
выполняет одно действие и возвращает {"ok": bool, "error": str|None}.
Вызывается из op_worker._exec_boost_*.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_CONNECT_TIMEOUT = 15.0
_DEFAULT_ACTION_TIMEOUT = 12.0

_COMMON_EMOJIS = ["❤", "🔥", "👍", "💯", "🎉", "👏", "😍", "🤩", "💪", "🥰"]


def parse_channel_ref(text: str) -> str:
    """Нормализовать ссылку на канал/чат → @username или числовой ID."""
    text = text.strip()
    # https://t.me/username or t.me/username
    m = re.search(r"t\.me/([A-Za-z0-9_]{3,})", text)
    if m:
        return f"@{m.group(1)}"
    # @username
    if text.startswith("@"):
        return text
    # numeric chat id
    if re.match(r"^-?\d+$", text):
        return text
    # bare username
    if re.match(r"^[A-Za-z0-9_]{3,}$", text):
        return f"@{text}"
    return text


def parse_msg_ids(text: str) -> list[int]:
    """Парсинг строки с ID сообщений: '123, 124-126, 130' → [123,124,125,126,130]."""
    ids: list[int] = []
    for part in re.split(r"[,;\s]+", text.strip()):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            ids.extend(range(a, min(b, a + 50) + 1))
        elif re.match(r"^\d+$", part):
            ids.append(int(part))
    return list(dict.fromkeys(ids))[:100]  # дедуп, макс 100


# ── Просмотры ─────────────────────────────────────────────────────────────────

async def boost_views(
    session_string: str,
    _acc: dict | None,
    channel: str,
    msg_ids: list[int],
) -> dict[str, Any]:
    """Накрутить просмотры сообщений канала с одного аккаунта."""
    from services.account_manager import _make_client
    from telethon.tl.functions.messages import GetMessagesViewsRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_DEFAULT_CONNECT_TIMEOUT)
        peer = await asyncio.wait_for(client.get_entity(channel), timeout=_DEFAULT_ACTION_TIMEOUT)
        await asyncio.wait_for(
            client(GetMessagesViewsRequest(peer=peer, id=msg_ids, increment=True)),
            timeout=_DEFAULT_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        err = str(exc)[:200]
        log.debug("boost_views channel=%s error: %s", channel, err)
        return {"ok": False, "error": err}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Реакции ───────────────────────────────────────────────────────────────────

async def boost_reaction(
    session_string: str,
    _acc: dict | None,
    channel: str,
    msg_id: int,
    emoji: str,
) -> dict[str, Any]:
    """Поставить реакцию на сообщение от одного аккаунта."""
    from services.account_manager import _make_client
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_DEFAULT_CONNECT_TIMEOUT)
        peer = await asyncio.wait_for(client.get_entity(channel), timeout=_DEFAULT_ACTION_TIMEOUT)
        await asyncio.wait_for(
            client(SendReactionRequest(
                peer=peer,
                msg_id=msg_id,
                reaction=[ReactionEmoji(emoticon=emoji)],
                add_to_recent=False,
            )),
            timeout=_DEFAULT_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        err = str(exc)[:200]
        log.debug("boost_reaction channel=%s msg=%d error: %s", channel, msg_id, err)
        return {"ok": False, "error": err}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Сторис ────────────────────────────────────────────────────────────────────

async def boost_stories(
    session_string: str,
    _acc: dict | None,
    target: str,
) -> dict[str, Any]:
    """Просмотреть все активные сторис пользователя/канала."""
    from services.account_manager import _make_client
    from telethon.tl.functions.stories import GetPeerStoriesRequest, IncrementStoryViewsRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_DEFAULT_CONNECT_TIMEOUT)
        peer = await asyncio.wait_for(client.get_entity(target), timeout=_DEFAULT_ACTION_TIMEOUT)
        peer_stories = await asyncio.wait_for(
            client(GetPeerStoriesRequest(peer=peer)),
            timeout=_DEFAULT_ACTION_TIMEOUT,
        )
        story_ids = [s.id for s in (peer_stories.stories.stories or [])]
        if not story_ids:
            return {"ok": True, "error": None, "stories_count": 0}
        await asyncio.wait_for(
            client(IncrementStoryViewsRequest(peer=peer, id=story_ids)),
            timeout=_DEFAULT_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None, "stories_count": len(story_ids)}
    except Exception as exc:
        err = str(exc)[:200]
        log.debug("boost_stories target=%s error: %s", target, err)
        return {"ok": False, "error": err}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def extract_flood_wait(exc: Exception, err_str: str) -> int:
    """Извлечь секунды FloodWait из исключения."""
    m = re.search(r"(?:flood.{0,10}wait|wait\s+of)\s+(\d+)", err_str, re.I)
    if m:
        return int(m.group(1))
    return 0
