"""«Мать-Дочка» — одноразовые дочерние группы поглощают бан-риск инвайта.

Массовый инвайт — самая баноопасная операция продукта (см. tg-manager/CLAUDE.md).
Вместо того чтобы приглашать аудиторию прямо в боевую группу (мать), можно
приглашать в одноразовую «дочернюю» группу, созданную под кампанию, с
закреплённым сообщением-редиректом на мать. Если Telegram закрывает дочернюю
группу — сгорает расходник, а не боевой канал.

Строим на уже существующих сервисах, не дублируем:
  - account_manager.create_channel — создание канала/супергруппы;
  - account_manager.create_channel_invite_link — invite-ссылка (без неё чужие
    аккаунты не резолвят только что созданный канал: у них нет его entity в
    кеше сессии, а get_entity по голому id без ссылки/username не работает);
  - mass_inviter_engine.classify_invite_error / "group error" в errors —
    уже отличает «чат закрыт» (ChatWriteForbiddenError/ChannelPrivateError) от
    «у этого аккаунта нет прав» (ChatAdminRequiredError) — на этом сигнале
    op_worker решает, когда дочернюю группу считать сгоревшей.

НЕ переиспользует services.brand_injection.post_welcome_and_pin — это
фиксированный рекламный pin продукта, редирект на мать — отдельное сообщение.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from services import account_manager

log = logging.getLogger(__name__)

# Предохранитель: сколько раз можно пересоздать сгоревшую дочернюю группу за
# один прогон _exec_mass_invite. Без потолка системная проблема (например,
# аккаунт-создатель без прав или бага классификации) плодила бы каналы без
# ограничения.
MAX_ROTATIONS_PER_RUN = 3


async def get_or_create_active(
    pool: asyncpg.Pool, owner_id: int, mother_ref: str, creator_acc: dict,
    redirect_ref: str = "",
) -> dict:
    """Активная дочерняя группа для (owner, mother) либо новая.

    `redirect_ref` — куда ведёт закреплённое сообщение. По умолчанию в мать; с
    витриной (services/showcase_layer) сюда передают ссылку на неё, и тогда люди
    попадают в буфер, а не всплеском в боевой канал. Пустое значение сохраняет
    прежнее поведение — существующие вызывающие не трогаем.

    Возвращает {"ok": True, "group_ref": str, "id": int} либо
    {"ok": False, "error": str}.
    """
    row = await pool.fetchrow(
        "SELECT id, group_ref FROM daughter_groups "
        "WHERE owner_id=$1 AND mother_ref=$2 AND status='active' "
        "ORDER BY created_at DESC LIMIT 1",
        owner_id, mother_ref,
    )
    if row:
        return {"ok": True, "group_ref": row["group_ref"], "id": int(row["id"])}
    return await _create_new(pool, owner_id, mother_ref, creator_acc, redirect_ref)


async def _create_new(pool: asyncpg.Pool, owner_id: int, mother_ref: str,
                      creator_acc: dict, redirect_ref: str = "") -> dict:
    res = await account_manager.create_channel(
        creator_acc["session_str"], "Chat", "", True, dict(creator_acc))
    if res.get("error") or not res.get("channel_id"):
        err = res.get("error") or "create_channel вернул пустой результат"
        log.warning("daughter_groups: создание канала не удалось: %s", err)
        return {"ok": False, "error": err}

    channel_id = int(res["channel_id"])
    access_hash = int(res.get("access_hash") or 0)

    link_res = await account_manager.create_channel_invite_link(
        creator_acc["session_str"], channel_id, dict(creator_acc), access_hash)
    if not link_res.get("ok") or not link_res.get("link"):
        err = link_res.get("error") or "ссылка не выпущена"
        log.warning("daughter_groups: ссылка на канал %s не выпущена: %s", channel_id, err)
        return {"ok": False, "error": err}
    group_ref = link_res["link"]

    # С витриной ведём в неё, без витрины — прямо в мать (прежнее поведение).
    _target = redirect_ref or mother_ref
    try:
        target_display = account_manager.format_telegram_join_ref_display(_target)
    except Exception:
        target_display = _target
    await _pin_mother_redirect(
        creator_acc["session_str"], channel_id, access_hash, target_display,
        dict(creator_acc))

    row = await pool.fetchrow(
        "INSERT INTO daughter_groups(owner_id, mother_ref, group_ref, channel_id, "
        "access_hash, creator_account_id, status) "
        "VALUES($1,$2,$3,$4,$5,$6,'active') RETURNING id",
        owner_id, mother_ref, group_ref, channel_id, access_hash, int(creator_acc["id"]),
    )
    return {"ok": True, "group_ref": group_ref, "id": int(row["id"])}


async def _pin_mother_redirect(
    session_string: str, channel_id: int, access_hash: int, mother_display: str, _acc: dict,
) -> bool:
    """Закрепить редирект на мать. Провал не критичен — группа остаётся рабочей
    (люди просто не увидят подсказку куда переходить), поэтому не критично."""
    # _OP_TIMEOUT — общий потолок одиночного запроса к Telegram: без него
    # half-open сокет мёртвого прокси вешает вызов навсегда.
    from services.account_manager import (_make_client, _CONNECT_TIMEOUT,
                                          _OP_TIMEOUT)

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        from telethon.tl.functions.messages import UpdatePinnedMessageRequest
        from telethon.tl.types import InputChannel

        channel = InputChannel(channel_id=channel_id, access_hash=access_hash)
        text = f"➡️ Основной канал: {mother_display}" if mother_display else "➡️ Переходите в основной канал по ссылке из описания"
        msg = await asyncio.wait_for(client.send_message(channel, text), timeout=_OP_TIMEOUT)
        await asyncio.wait_for(
            client(UpdatePinnedMessageRequest(peer=channel, id=msg.id, silent=True)),
            timeout=_OP_TIMEOUT)
        return True
    except Exception as e:
        log.warning("daughter_groups: pin redirect failed channel=%s: %s", channel_id, e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def mark_burned(pool: asyncpg.Pool, daughter_id: int, reason: str) -> None:
    await pool.execute(
        "UPDATE daughter_groups SET status='burned', burned_at=now(), burn_reason=$2 "
        "WHERE id=$1 AND status='active'",
        daughter_id, (reason or "")[:200],
    )
