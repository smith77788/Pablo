"""Content Cloner Engine — копирует/пересылает сообщения из канала-источника в каналы-цели."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    UserBannedInChannelError,
    ChatAdminRequiredError,
    MessageIdInvalidError,
)

from services.account_manager import _make_client

log = logging.getLogger(__name__)

_FLOOD_BASE = 2
_INTER_MSG_DELAY = 1.5   # секунды между постами в copy-режиме


def parse_channel_ref(text: str) -> str:
    """Нормализует ссылку/username канала."""
    text = text.strip()
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]+)", text)
    if m:
        return f"@{m.group(1)}"
    if text.lstrip("-").isdigit():
        return text
    if re.match(r"^[A-Za-z0-9_]{3,}$", text):
        return f"@{text}"
    return text


async def clone_to_channel(
    session_string: str,
    acc: dict[str, Any],
    source_ref: str,
    target_ref: str,
    msg_ids: list[int],
    mode: str,        # "copy" или "forward"
) -> dict[str, Any]:
    """
    Копирует/пересылает список сообщений msg_ids из source_ref в target_ref.
    mode="forward" — пересылает с сохранением подписи источника.
    mode="copy"    — скачивает медиа и постит заново без attribution.
    Возвращает {"ok": int, "fail": int, "errors": [...]}.
    """
    device = {
        "device_model":    acc.get("device_model", "Infragram"),
        "system_version":  acc.get("system_version", "1.0"),
        "app_version":     acc.get("app_version", "1.0"),
        "lang_code":       acc.get("lang_code", "en"),
        "system_lang_code": acc.get("system_lang_code", "en"),
        "proxy_url":       acc.get("proxy_url") or "",
    }

    client = _make_client(session_string, device)
    result: dict[str, Any] = {"ok": 0, "fail": 0, "errors": []}

    try:
        await client.connect()
        if not await client.is_user_authorized():
            result["errors"].append("Сессия истекла")
            result["fail"] = len(msg_ids)
            return result

        try:
            source_entity = await client.get_entity(source_ref)
        except Exception as exc:
            result["errors"].append(f"Нет доступа к источнику: {exc}")
            result["fail"] = len(msg_ids)
            return result

        try:
            target_entity = await client.get_entity(target_ref)
        except Exception as exc:
            result["errors"].append(f"Нет доступа к цели {target_ref}: {exc}")
            result["fail"] = len(msg_ids)
            return result

        if mode == "forward":
            batch_size = 50
            for i in range(0, len(msg_ids), batch_size):
                batch = msg_ids[i: i + batch_size]
                try:
                    await client.forward_messages(target_entity, batch, source_entity)
                    result["ok"] += len(batch)
                except FloodWaitError as e:
                    wait = e.seconds + _FLOOD_BASE
                    log.warning("content_cloner: FloodWait %ds", wait)
                    await asyncio.sleep(wait)
                    try:
                        await client.forward_messages(target_entity, batch, source_entity)
                        result["ok"] += len(batch)
                    except Exception as exc2:
                        result["fail"] += len(batch)
                        result["errors"].append(str(exc2)[:120])
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError) as exc:
                    result["fail"] += len(batch)
                    result["errors"].append(f"Нет прав в {target_ref}: {exc}")
                    break
                except MessageIdInvalidError:
                    result["fail"] += len(batch)
                    result["errors"].append("Сообщения недоступны")
                except Exception as exc:
                    result["fail"] += len(batch)
                    result["errors"].append(str(exc)[:120])
                await asyncio.sleep(0.5)

        else:  # copy mode
            for msg_id in msg_ids:
                try:
                    msgs = await client.get_messages(source_entity, ids=msg_id)
                    msg = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
                    if msg is None:
                        result["fail"] += 1
                        continue

                    text = getattr(msg, "message", "") or ""
                    fmt_entities = getattr(msg, "entities", None)
                    media = getattr(msg, "media", None)

                    if media is not None:
                        file_bytes = await client.download_media(msg, file=bytes)
                        await client.send_file(
                            target_entity,
                            file=file_bytes,
                            caption=text,
                            formatting_entities=fmt_entities,
                        )
                    elif text:
                        await client.send_message(
                            target_entity,
                            message=text,
                            formatting_entities=fmt_entities,
                        )
                    else:
                        result["fail"] += 1
                        continue

                    result["ok"] += 1
                    await asyncio.sleep(_INTER_MSG_DELAY)

                except FloodWaitError as e:
                    wait = e.seconds + _FLOOD_BASE
                    log.warning("content_cloner copy: FloodWait %ds msg=%d", wait, msg_id)
                    await asyncio.sleep(wait)
                    result["fail"] += 1
                    result["errors"].append(f"FloodWait {wait}s msg {msg_id}")
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError) as exc:
                    result["fail"] += 1
                    result["errors"].append(f"Нет прав: {exc}")
                    break
                except Exception as exc:
                    result["fail"] += 1
                    result["errors"].append(f"msg {msg_id}: {str(exc)[:100]}")

    except Exception as exc:
        log.exception("content_cloner: fatal")
        result["fail"] += len(msg_ids) - result["ok"]
        result["errors"].append(str(exc)[:150])
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


async def get_last_msg_ids(
    session_string: str,
    acc: dict[str, Any],
    source_ref: str,
    count: int,
) -> list[int]:
    """Возвращает ID последних count сообщений из канала-источника."""
    device = {
        "device_model":    acc.get("device_model", "Infragram"),
        "system_version":  acc.get("system_version", "1.0"),
        "app_version":     acc.get("app_version", "1.0"),
        "lang_code":       acc.get("lang_code", "en"),
        "system_lang_code": acc.get("system_lang_code", "en"),
        "proxy_url":       acc.get("proxy_url") or "",
    }
    client = _make_client(session_string, device)
    ids: list[int] = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return ids
        entity = await client.get_entity(source_ref)
        msgs = await client.get_messages(entity, limit=count)
        ids = [m.id for m in msgs if m is not None]
    except Exception as exc:
        log.warning("content_cloner get_last_msg_ids: %s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return ids
