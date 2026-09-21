"""Content Cloner Engine — копирует/пересылает сообщения из канала-источника в каналы-цели."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    UserBannedInChannelError,
    ChatAdminRequiredError,
    MessageIdInvalidError,
)

from services.account_manager import _make_client
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_FLOOD_BASE = 2
_INTER_MSG_DELAY = 1.5   # секунды между постами в copy-режиме

# ── Потолок на флуд-сон ВНУТРИ клонирования ──────────────────────────────────
# Telegram отдаёт FloodWait на forward/send и на минуты, и на часы. Сон ровно на
# столько держит слот исполнителя, арендованный аккаунт и живой коннект к
# Telegram всё это время: операция не двигается, а аккаунт остаётся подключённым
# без единого запроса — ровно то поведение, от которого сессию и отзывают.
#
# Короткую паузу дешевле переждать на месте. Длинную — НЕ ждём: останавливаем
# клонирование и отдаём наверх настоящий flood_wait, чтобы исполнитель поставил
# аккаунт на cooldown и перенёс операцию.
_MAX_FLOOD_INLINE = 60


def _build_device(acc: dict[str, Any]) -> dict[str, Any]:
    """Словарь аккаунта для _make_client — С ПОЛЯМИ ТРАНСПОРТА.

    Раньше здесь собирался словарь из шести полей отображения (device_model,
    versions, lang, proxy_url). Транспорт же выбирается по `id`, `proxy_id` и
    `cf_relay_url`, и без них клиент уходил НАПРЯМУЮ с host-IP, пока остальные
    подсистемы того же аккаунта шли через назначенный прокси или релей. Одна
    сессия с двух адресов — это AUTH_KEY_DUPLICATED и отозванный ключ.

    Исполнитель отдаёт полную строку аккаунта, поэтому берём её целиком и лишь
    подставляем значения по умолчанию там, где поле пустое.
    """
    device = dict(acc or {})
    for key, default in (
        ("device_model", "Infragram"),
        ("system_version", "1.0"),
        ("app_version", "1.0"),
        ("lang_code", "en"),
        ("system_lang_code", "en"),
    ):
        if not device.get(key):
            device[key] = default
    device["proxy_url"] = device.get("proxy_url") or ""
    return device


def parse_channel_ref(text: str) -> str:
    """Нормализует ссылку/username канала → @username, числовой ID, или
    канонический https://t.me/+HASH для приватных invite-ссылок.

    Delegates to account_manager.normalize_telegram_join_ref — the previous
    regex here matched "t.me/joinchat/HASH" as if "joinchat" were a public
    username, and didn't match "t.me/+HASH" at all, silently corrupting
    private invite links.
    """
    text = text.strip()
    if text.lstrip("-").isdigit():
        return text  # numeric chat ID — not a join ref, pass through as-is

    from services.account_manager import format_telegram_join_ref_display

    formatted = format_telegram_join_ref_display(text)
    return formatted or text


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
    device = _build_device(acc)

    client = _make_client(session_string, device)
    # flood_wait/peer_flood — наверх, исполнителю: без них он не поставит аккаунт
    # на cooldown и пойдёт следующей целью тем же флудящим аккаунтом.
    result: dict[str, Any] = {
        "ok": 0, "fail": 0, "errors": [], "flood_wait": 0, "peer_flood": False,
    }

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
                except PeerFloodError:
                    # Спам-блок аккаунта — не ретраим и не идём дальше.
                    result["peer_flood"] = True
                    result["fail"] += len(batch)
                    result["errors"].append("peer flood — аккаунт ограничен")
                    break
                except FloodWaitError as e:
                    _fw = int(getattr(e, "seconds", 0) or 0)
                    if _fw > _MAX_FLOOD_INLINE:
                        log.warning(
                            "content_cloner: длинный FloodWait %ds — стоп, отдаём наверх",
                            _fw,
                        )
                        result["flood_wait"] = _fw
                        result["fail"] += len(batch)
                        result["errors"].append(f"flood wait {_fw}s")
                        break
                    log.warning("content_cloner: FloodWait %ds", _fw)
                    await asyncio.sleep(_fw + _FLOOD_BASE)
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

                except PeerFloodError:
                    result["peer_flood"] = True
                    result["fail"] += 1
                    result["errors"].append("peer flood — аккаунт ограничен")
                    break
                except FloodWaitError as e:
                    _fw = int(getattr(e, "seconds", 0) or 0)
                    result["fail"] += 1
                    if _fw > _MAX_FLOOD_INLINE:
                        log.warning(
                            "content_cloner copy: длинный FloodWait %ds msg=%d — стоп",
                            _fw, msg_id,
                        )
                        result["flood_wait"] = _fw
                        result["errors"].append(f"flood wait {_fw}s")
                        break
                    log.warning("content_cloner copy: FloodWait %ds msg=%d", _fw, msg_id)
                    await asyncio.sleep(_fw + _FLOOD_BASE)
                    result["errors"].append(f"FloodWait {_fw}s msg {msg_id}")
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError) as exc:
                    result["fail"] += 1
                    result["errors"].append(f"Нет прав: {exc}")
                    break
                except Exception as exc:
                    result["fail"] += 1
                    result["errors"].append(f"msg {msg_id}: {str(exc)[:100]}")

    except Exception as exc:
        # Флуд на подключении/резолве не должен уходить наверх как обычная
        # ошибка с flood_wait=0 — иначе исполнитель не поставит cooldown.
        _en = type(exc).__name__
        if _en == "PeerFloodError":
            result["peer_flood"] = True
        elif _en == "FloodWaitError":
            result["flood_wait"] = int(getattr(exc, "seconds", 0) or 0)
        log.exception("content_cloner: fatal")
        result["fail"] += len(msg_ids) - result["ok"]
        result["errors"].append(str(exc)[:150])
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "clone_to_channel: disconnect")

    return result


async def get_last_msg_ids(
    session_string: str,
    acc: dict[str, Any],
    source_ref: str,
    count: int,
) -> list[int]:
    """Возвращает ID последних count сообщений из канала-источника."""
    device = _build_device(acc)
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
        except Exception as e:
            log_exc_swallow(log, "get_last_msg_ids: disconnect")
    return ids
