"""Content Mesh — automated content distribution from source channels to targets.

Each mesh watches a source Telegram channel for new posts and redistributes
them (as reposts, not forwards) to all enabled target channels with a
configurable delay.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import asyncpg
from aiogram import Bot
from telethon.errors import (
    AuthKeyError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    UserDeactivatedBanError,
    UserNotParticipantError,
)

from services.account_manager import _connect_and_track, _make_client

log = logging.getLogger(__name__)

# Потолок одиночного запроса к Telegram в этом фоновом цикле.
#
# Здесь он не «на всякий случай»: цикл держит аккаунт через единый арбитр
# op_worker.try_claim_account, а отпускает его в finally. Повисший запрос
# (мёртвый прокси отдаёт half-open сокет: TCP есть, ответа нет и не будет) из
# finally не возвращается НИКОГДА, поэтому аккаунт остаётся в
# op_worker._accounts_in_use, а renew_leases() продлевает его аренду каждые
# 30 секунд — вечно. Реконсилер такую строку не чистит намеренно: для него
# живая память держателя и есть доказательство занятости. Итог — аккаунт
# навсегда «занят операцией»: он выпадает и из операций владельца, и из
# прогрева, и из призрака, и заметить это можно только по счётчику флота.
#
# 45 секунд — щедро: живой Telegram отвечает за секунды. Занижать нельзя,
# ложный таймаут уводит действие в повтор, то есть в лишний запрос по Telegram.
_TG_TIMEOUT = 45

# Перенос медиа — отдельный случай: видео законно едет минутами, общий потолок
# обрывал бы здоровую загрузку.
_MEDIA_TIMEOUT = 300

_LOOP_INTERVAL = 120    # seconds between full sweeps
_MAX_NEW_PER_CYCLE = 10 # max new source messages to enqueue per cycle per mesh


# ─── Source polling ───────────────────────────────────────────────────────────


async def _poll_source(pool: asyncpg.Pool, mesh: asyncpg.Record) -> None:
    """Check source channel for new messages and enqueue them for targets."""
    mesh_id = mesh["id"]
    source_channel = mesh["source_channel"]
    account_id = mesh["source_account_id"]
    last_id = mesh["last_post_id"] or 0
    delay = mesh["delay_minutes"]

    acc = await pool.fetchrow(
        """
        SELECT a.id, a.session_str, a.cooldown_until,
               a.device_model, a.system_version, a.app_version,
               a.lang_code, a.system_lang_code,
               p.proxy_url
        FROM tg_accounts a
        LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
        WHERE a.id = $1 AND COALESCE(a.acc_status,'active') NOT IN ('banned','deactivated','session_expired')
        """,
        account_id,
    )
    if not acc:
        return

    session = acc["session_str"]
    if not session:
        return
    targets = await pool.fetch(
        "SELECT * FROM mesh_targets WHERE mesh_id=$1 AND enabled=TRUE",
        mesh_id,
    )
    if not targets:
        return

    # Атомарный захват ДО коннекта: одна сессия не коннектится mesh-циклом И
    # операцией одновременно = AUTH_KEY_DUPLICATED. Освобождаем в finally.
    _opw = None
    _cm_leased = True
    try:
        from services import op_worker as _opw_mod
        _opw = _opw_mod
        _cm_leased = await _opw.try_claim_account(int(account_id))
    except Exception:
        _opw = None
        _cm_leased = True
    if not _cm_leased:
        return

    acc_dict = dict(acc)
    try:
        client = _make_client(session, acc_dict)
        # Коннект — через канонический помощник account_manager: у `async with client`
        # потолка нет вовсе (он зовёт client.start()), а аккаунт здесь уже захвачен
        # у арбитра op_worker — повисшее рукопожатие держало бы его занятым вечно.
        # Бонусом помощник пишет успех прокси в infra_memory, чего `async with` не делал.
        await _connect_and_track(client, acc_dict, "mesh_poll")
        try:
            entity = await asyncio.wait_for(client.get_entity(source_channel), timeout=_TG_TIMEOUT)
            messages = await asyncio.wait_for(client.get_messages(entity, limit=_MAX_NEW_PER_CYCLE, min_id=last_id), timeout=_TG_TIMEOUT)

            if not messages:
                return

            new_max_id = last_id
            now = datetime.now(timezone.utc)

            for msg in reversed(messages):  # oldest first
                if msg.id <= last_id:
                    continue
                new_max_id = max(new_max_id, msg.id)

                # Skip service messages (no text, no media)
                if not msg.text and not msg.media:
                    continue

                # Enqueue delivery to each target
                scheduled = now + timedelta(minutes=delay)
                for target in targets:
                    existing = await pool.fetchrow(
                        "SELECT id FROM mesh_queue WHERE mesh_id=$1 AND target_id=$2 AND source_msg_id=$3",
                        mesh_id, target["id"], msg.id,
                    )
                    if existing:
                        continue
                    await pool.execute(
                        """
                        INSERT INTO mesh_queue (mesh_id, target_id, source_msg_id, scheduled_at)
                        VALUES ($1, $2, $3, $4)
                        """,
                        mesh_id, target["id"], msg.id, scheduled,
                    )

            if new_max_id > last_id:
                await pool.execute(
                    "UPDATE content_meshes SET last_post_id=$1, updated_at=NOW() WHERE id=$2",
                    new_max_id, mesh_id,
                )
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.debug("Content Mesh: disconnect failed")

    except FloodWaitError as e:
        from services import flood_engine as _fe

        await _fe.note_flood(pool, account_id, e, "mesh_poll")
        log.debug("Content Mesh: flood wait %ds for mesh %d source poll", e.seconds, mesh_id)
    except (UserDeactivatedBanError, AuthKeyError):
        log.warning("Content Mesh: account %d banned, disabling mesh %d", account_id, mesh_id)
        await pool.execute("UPDATE content_meshes SET enabled=FALSE WHERE id=$1", mesh_id)
    except (ChannelPrivateError, UserNotParticipantError) as e:
        log.debug("Content Mesh: can't access source %s for mesh %d: %s", source_channel, mesh_id, e)
    except Exception as e:
        log.debug("Content Mesh: source poll error for mesh %d: %s", mesh_id, e)
    finally:
        if _opw and _cm_leased:
            try:
                await _opw.release_accounts([int(account_id)])
            except Exception:
                log.debug("Content Mesh: release failed for account %d", account_id)


# ─── Queue processing ─────────────────────────────────────────────────────────


async def _process_delivery(pool: asyncpg.Pool, item: asyncpg.Record) -> None:
    """Send one queued message to its target."""
    mesh = await pool.fetchrow(
        "SELECT * FROM content_meshes WHERE id=$1", item["mesh_id"]
    )
    if not mesh or not mesh["enabled"]:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='mesh_disabled' WHERE id=$1", item["id"])
        return

    target = await pool.fetchrow(
        "SELECT * FROM mesh_targets WHERE id=$1 AND enabled=TRUE", item["target_id"]
    )
    if not target:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='target_disabled' WHERE id=$1", item["id"])
        return

    account_id = mesh["source_account_id"]
    acc = await pool.fetchrow(
        """SELECT a.id, a.session_str, a.cooldown_until,
                  a.device_model, a.system_version, a.app_version,
                  a.lang_code, a.system_lang_code,
                  p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.id = $1 AND COALESCE(a.acc_status,'active') NOT IN ('banned','deactivated','session_expired')""",
        account_id,
    )
    if not acc:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='no_account' WHERE id=$1", item["id"])
        return

    session = acc["session_str"]
    if not session:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='no_session' WHERE id=$1", item["id"])
        return
    # Anti-detection (#7): не репостим через аккаунт в карантине (недавний
    # флуд/блок/ограничение). acc_status выше ловит только banned/deactivated —
    # единый пульс здоровья ловит ещё и флуд/cooldown. Откладываем доставку
    # (не error: аккаунт восстановится), fail-open — сбой сигнала не блокирует.
    try:
        from services.infra_memory import is_account_quarantined

        if await is_account_quarantined(pool, account_id):
            await pool.execute(
                "UPDATE mesh_queue SET scheduled_at = NOW() + INTERVAL '15 minutes' WHERE id=$1",
                item["id"],
            )
            log.info(
                "content_mesh: acc %d в карантине — доставка item %d отложена на 15м",
                account_id, item["id"],
            )
            return
    except Exception as _qe:
        log.debug("content_mesh: quarantine check failed (fail-open): %s", _qe)

    # Атомарный захват ДО коннекта. Занят операцией → откладываем доставку (не
    # error: восстановится), никакой второй сессии на одном auth-key.
    _opw = None
    _cm_leased = True
    try:
        from services import op_worker as _opw_mod
        _opw = _opw_mod
        _cm_leased = await _opw.try_claim_account(int(account_id))
    except Exception:
        _opw = None
        _cm_leased = True
    if not _cm_leased:
        await pool.execute(
            "UPDATE mesh_queue SET scheduled_at = NOW() + INTERVAL '5 minutes' WHERE id=$1",
            item["id"])
        return

    try:
        acc_dict = dict(acc)
        client = _make_client(session, acc_dict)
        # Коннект — через канонический помощник account_manager: у `async with client`
        # потолка нет вовсе (он зовёт client.start()), а аккаунт здесь уже захвачен
        # у арбитра op_worker — повисшее рукопожатие держало бы его занятым вечно.
        # Бонусом помощник пишет успех прокси в infra_memory, чего `async with` не делал.
        await _connect_and_track(client, acc_dict, "mesh_deliver")
        try:
            source_entity = await asyncio.wait_for(client.get_entity(mesh["source_channel"]), timeout=_TG_TIMEOUT)
            source_msgs = await asyncio.wait_for(client.get_messages(source_entity, ids=item["source_msg_id"]), timeout=_TG_TIMEOUT)
            if not source_msgs:
                await pool.execute(
                    "UPDATE mesh_queue SET status='error', error_msg='source_msg_not_found', sent_at=NOW() WHERE id=$1",
                    item["id"],
                )
                return

            msg = source_msgs if not isinstance(source_msgs, list) else source_msgs[0]
            if not msg:
                await pool.execute(
                    "UPDATE mesh_queue SET status='error', error_msg='source_msg_none', sent_at=NOW() WHERE id=$1",
                    item["id"],
                )
                return

            target_entity = await asyncio.wait_for(client.get_entity(target["target_channel"]), timeout=_TG_TIMEOUT)

            # Build text with optional CTA
            text = msg.text or ""
            if mesh["append_text"]:
                text = (text + "\n\n" + mesh["append_text"]).strip()

            if msg.media and not text:
                await asyncio.wait_for(client.send_file(target_entity, msg.media), timeout=_MEDIA_TIMEOUT)
            elif msg.media:
                await asyncio.wait_for(client.send_file(target_entity, msg.media, caption=text), timeout=_MEDIA_TIMEOUT)
            else:
                await asyncio.wait_for(client.send_message(target_entity, text), timeout=_TG_TIMEOUT)
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.debug("Content Mesh: disconnect failed")

        await pool.execute(
            "UPDATE mesh_queue SET status='sent', sent_at=NOW() WHERE id=$1", item["id"]
        )

    except FloodWaitError as e:
        # Перенести доставку мало: пауза касается АККАУНТА, а не только этой
        # очереди, и без записи в пульс его тут же возьмёт другая подсистема.
        from services import flood_engine as _fe

        await _fe.note_flood(pool, account_id, e, "mesh_send")
        # Reschedule rather than fail
        new_time = datetime.now(timezone.utc) + timedelta(seconds=e.seconds + 30)
        await pool.execute(
            "UPDATE mesh_queue SET scheduled_at=$1 WHERE id=$2",
            new_time, item["id"],
        )
    except (ChatWriteForbiddenError, ChannelPrivateError) as e:
        await pool.execute(
            "UPDATE mesh_queue SET status='error', error_msg=$1, sent_at=NOW() WHERE id=$2",
            str(e)[:200], item["id"],
        )
        await pool.execute(
            "UPDATE mesh_targets SET enabled=FALSE WHERE id=$1", item["target_id"]
        )
    except (UserDeactivatedBanError, AuthKeyError):
        await pool.execute(
            "UPDATE mesh_queue SET status='error', error_msg='account_banned', sent_at=NOW() WHERE id=$1",
            item["id"],
        )
        await pool.execute("UPDATE content_meshes SET enabled=FALSE WHERE id=$1", item["mesh_id"])
    except Exception as e:
        await pool.execute(
            "UPDATE mesh_queue SET status='error', error_msg=$1, sent_at=NOW() WHERE id=$2",
            str(e)[:200], item["id"],
        )
    finally:
        if _opw and _cm_leased:
            try:
                await _opw.release_accounts([int(account_id)])
            except Exception:
                log.debug("content_mesh: release failed for account %d", account_id)


# ─── Main loop ────────────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    log.info("Content Mesh started")
    while True:
        try:
            # Poll sources for new content
            meshes = await pool.fetch(
                """
                SELECT * FROM content_meshes
                WHERE enabled = TRUE
                  AND source_channel IS NOT NULL
                  AND source_account_id IS NOT NULL
                ORDER BY id
                """
            )
            for mesh in meshes:
                try:
                    await _poll_source(pool, mesh)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("Content Mesh: poll error mesh %d: %s", mesh["id"], e)
                await asyncio.sleep(5)

            # Process pending deliveries
            pending = await pool.fetch(
                """
                SELECT * FROM mesh_queue
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at
                LIMIT 50
                """
            )
            for item in pending:
                try:
                    await _process_delivery(pool, item)
                except Exception as e:
                    log.debug("Content Mesh: delivery error item %d: %s", item["id"], e)
                await asyncio.sleep(3)

        except Exception as e:
            log.error("Content Mesh loop error: %s", e)

        await asyncio.sleep(_LOOP_INTERVAL)
