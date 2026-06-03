"""
Activity Engine — органическая активность в собственных ресурсах.

В отличие от account_warmer (разогрев через публичные каналы),
этот движок работает с СОБСТВЕННЫМИ ресурсами пользователя:
каналы, группы, боты. Создаёт органическую активность:
читает посты, ставит реакции, оставляет комментарии.

Профили активности:
- reader    — чтение, просмотр, ReadHistory (безопасно, низкий риск)
- commenter — комментарии в discussion group (средний риск)
- reactor   — реакции на посты (средний риск)
- mixed     — все типы в пропорции

Адаптивный пейсинг: при FloodWait удваивает паузы до 8× от базового.
Кросс-аккаунтное взаимодействие: несколько аккаунтов → те же ресурсы.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import asyncpg

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_REACTIONS = ["👍", "❤️", "🔥", "🎉", "👏", "😍", "💯", "🤩", "😂", "🏆"]
_COMMENT_TEXTS = [
    "👍", "Спасибо!", "Интересно", "Согласен",
    "Хорошая тема", "Полезная информация", "Да, именно",
    "Отличный материал!", "Актуально", "👏",
    "Интересная точка зрения", "Благодарю за пост",
    "Продолжайте", "Поддерживаю", "Спасибо за контент",
    "Очень полезно", "🔥", "Хороший контент", "Важная тема",
]

_FATAL_ERRORS = frozenset({
    "UserDeactivatedBanError",
    "AuthKeyUnregisteredError",
    "PhoneNumberBannedError",
    "SessionRevokedError",
})

# Веса действий по профилям (action, weight)
_PROFILE_ACTIONS: dict[str, list[tuple[str, int]]] = {
    "reader": [
        ("read_resource", 50),
        ("mark_read", 30),
        ("view_profile", 20),
    ],
    "commenter": [
        ("read_resource", 25),
        ("send_comment", 45),
        ("forward_to_saved", 30),
    ],
    "reactor": [
        ("read_resource", 30),
        ("send_reaction", 50),
        ("vote_poll", 20),
    ],
    "mixed": [
        ("read_resource", 25),
        ("send_reaction", 25),
        ("send_comment", 20),
        ("mark_read", 15),
        ("forward_to_saved", 10),
        ("vote_poll", 5),
    ],
}

_PROFILE_LABELS = {
    "reader":    "📖 Reader — чтение и просмотр (низкий риск)",
    "commenter": "💬 Commenter — акцент на комментарии",
    "reactor":   "❤️ Reactor — акцент на реакции",
    "mixed":     "🔀 Mixed — все типы действий",
}

_ACTION_LABELS = {
    "read_resource":   "📖 Читал ресурс",
    "mark_read":       "✅ Отметил прочитанным",
    "view_profile":    "👁 Просматривал инфо",
    "send_reaction":   "❤️ Поставил реакцию",
    "send_comment":    "💬 Оставил комментарий",
    "forward_to_saved":"📌 Сохранил пост",
    "vote_poll":       "📊 Проголосовал",
}


def _pick_action(profile: str) -> str:
    """Выбирает действие по весам профиля."""
    options = _PROFILE_ACTIONS.get(profile, _PROFILE_ACTIONS["mixed"])
    total = sum(w for _, w in options)
    r = random.randint(1, total)
    cumulative = 0
    for action, weight in options:
        cumulative += weight
        if r <= cumulative:
            return action
    return options[0][0]


async def get_own_resources(pool: asyncpg.Pool, owner_id: int,
                            refs: Optional[list[str]] = None) -> list[dict]:
    """Загружает собственные ресурсы пользователя как цели активности."""
    resources: list[dict] = []
    try:
        if refs:
            for ref in refs:
                resources.append({"ref": ref, "type": "channel"})
            return resources

        channels = await pool.fetch(
            """SELECT DISTINCT
                  COALESCE('@'||username, channel_id::text) AS ref,
                  COALESCE(title, username, 'id'||channel_id::text) AS label,
                  'channel' AS rtype
               FROM managed_channels
               WHERE owner_id=$1
               LIMIT 15""",
            owner_id,
        )
        bots = await pool.fetch(
            """SELECT DISTINCT '@'||username AS ref,
                  COALESCE(first_name, username) AS label,
                  'bot' AS rtype
               FROM managed_bots
               WHERE added_by=$1 AND is_active=TRUE
                 AND username IS NOT NULL AND username != ''
               LIMIT 5""",
            owner_id,
        )
        for r in channels:
            resources.append({"ref": r["ref"], "type": r["rtype"], "label": r["label"]})
        for r in bots:
            resources.append({"ref": r["ref"], "type": r["rtype"], "label": r["label"]})
    except Exception as e:
        log.debug("activity_engine.get_own_resources owner=%d: %s", owner_id, e)
    return resources


async def _act_read(client, ref: str) -> bool:
    try:
        entity = await client.get_entity(ref)
        limit = random.randint(5, 12)
        msgs = await client.get_messages(entity, limit=limit)
        for _ in msgs:
            await asyncio.sleep(random.uniform(1.0, 3.0))
        await asyncio.sleep(random.uniform(2, 6))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity read %s", ref)
        return False


async def _act_mark_read(client, ref: str) -> bool:
    try:
        from telethon.tl.functions.messages import ReadHistoryRequest
        entity = await client.get_entity(ref)
        msgs = await client.get_messages(entity, limit=5)
        if msgs:
            await client(ReadHistoryRequest(peer=entity, max_id=msgs[0].id))
        await asyncio.sleep(random.uniform(1, 3))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity mark_read %s", ref)
        return False


async def _act_react(client, ref: str) -> bool:
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        entity = await client.get_entity(ref)
        msgs = await client.get_messages(entity, limit=10)
        if not msgs:
            return False
        msg = random.choice(list(msgs))
        emoticon = random.choice(_REACTIONS)
        await client(SendReactionRequest(
            peer=entity, msg_id=msg.id,
            reaction=[ReactionEmoji(emoticon=emoticon)],
        ))
        await asyncio.sleep(random.uniform(1, 4))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity react %s", ref)
        return False


async def _act_comment(client, ref: str) -> bool:
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        entity = await client.get_entity(ref)
        if not getattr(entity, "broadcast", False):
            return False
        full = await client(GetFullChannelRequest(entity))
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if not linked_id:
            return False
        msgs = await client.get_messages(entity, limit=15)
        candidates = [m for m in msgs if m.replies and m.replies.replies >= 0]
        if not candidates:
            return False
        post = random.choice(candidates[:5])
        comment = random.choice(_COMMENT_TEXTS)
        discussion = await client.get_entity(linked_id)
        await client.send_message(discussion, comment, comment_to=post.id)
        await asyncio.sleep(random.uniform(5, 15))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity comment %s", ref)
        return False


async def _act_forward(client, ref: str) -> bool:
    try:
        entity = await client.get_entity(ref)
        msgs = await client.get_messages(entity, limit=15)
        if not msgs:
            return False
        candidates = [m for m in msgs if m.media or (m.text and len(m.text or "") > 40)]
        msg = random.choice(candidates if candidates else list(msgs))
        await client.forward_messages("me", msg)
        await asyncio.sleep(random.uniform(2, 6))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity forward %s", ref)
        return False


async def _act_vote(client, ref: str) -> bool:
    try:
        from telethon.tl.functions.messages import SendVoteRequest
        from telethon.tl.types import MessageMediaPoll
        entity = await client.get_entity(ref)
        msgs = await client.get_messages(entity, limit=25)
        polls = [m for m in msgs
                 if isinstance(m.media, MessageMediaPoll) and not m.media.poll.closed]
        if not polls:
            return False
        msg = random.choice(polls)
        options = msg.media.poll.answers
        if not options:
            return False
        chosen = random.choice(options)
        await client(SendVoteRequest(peer=entity, msg_id=msg.id, options=[chosen.option]))
        await asyncio.sleep(random.uniform(2, 5))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity vote %s", ref)
        return False


async def _act_view_profile(client, ref: str) -> bool:
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        entity = await client.get_entity(ref)
        await client(GetFullChannelRequest(entity))
        await asyncio.sleep(random.uniform(3, 7))
        return True
    except Exception as e:
        if type(e).__name__ in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "activity view_profile %s", ref)
        return False


async def run_resource_activity_session(pool: asyncpg.Pool, session: dict) -> dict:
    """
    Выполняет один день активности в ресурсах для всех аккаунтов сессии.
    Возвращает {'actions_done', 'actions_ok', 'actions_fail', 'completed'}.
    """
    from services import account_manager

    sess_id   = session["id"]
    owner_id  = session["owner_id"]
    acc_ids: list   = session.get("account_ids") or []
    res_refs: list  = session.get("resource_refs") or []
    profile   = session.get("profile_type", "mixed")
    daily     = session.get("daily_actions", 8)
    target_d  = session.get("target_days", 14)
    current_d = session.get("current_day", 0)

    if not acc_ids:
        log.warning("activity_session %d: no accounts", sess_id)
        return {"actions_done": 0, "actions_ok": 0, "actions_fail": 0, "completed": False}

    resources = await get_own_resources(pool, owner_id, res_refs if res_refs else None)
    if not resources:
        log.warning("activity_session %d: no resources found for owner=%d", sess_id, owner_id)
        return {"actions_done": 0, "actions_ok": 0, "actions_fail": 0, "completed": False}

    actions_per_acc = max(1, daily // len(acc_ids))
    total_ok = 0
    total_fail = 0

    for acc_id in acc_ids:
        acc_row = await pool.fetchrow(
            """SELECT a.session_str, a.device_model, a.system_version, a.app_version, p.proxy_url
               FROM tg_accounts a
               LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
               WHERE a.id=$1 AND a.is_active=TRUE""",
            acc_id,
        )
        if not acc_row or not acc_row["session_str"]:
            continue

        device = dict(acc_row) if acc_row["device_model"] else None
        client = account_manager._make_client(acc_row["session_str"], device)

        flood_multiplier = 1.0
        base_delay = random.uniform(12, 35)

        try:
            await asyncio.wait_for(client.connect(), timeout=15)

            for i in range(actions_per_acc):
                action   = _pick_action(profile)
                resource = resources[i % len(resources)]
                ref      = resource["ref"]
                success  = False
                error_str: Optional[str] = None
                t0 = time.monotonic()

                try:
                    if action == "read_resource":
                        success = await asyncio.wait_for(_act_read(client, ref), timeout=60)
                    elif action == "mark_read":
                        success = await asyncio.wait_for(_act_mark_read(client, ref), timeout=30)
                    elif action == "send_reaction":
                        success = await asyncio.wait_for(_act_react(client, ref), timeout=30)
                    elif action == "send_comment":
                        success = await asyncio.wait_for(_act_comment(client, ref), timeout=90)
                    elif action == "forward_to_saved":
                        success = await asyncio.wait_for(_act_forward(client, ref), timeout=60)
                    elif action == "vote_poll":
                        success = await asyncio.wait_for(_act_vote(client, ref), timeout=30)
                    elif action == "view_profile":
                        success = await asyncio.wait_for(_act_view_profile(client, ref), timeout=30)
                    else:
                        success = True

                except asyncio.TimeoutError:
                    error_str = "timeout"
                    success = False
                except Exception as exc:
                    etype = type(exc).__name__
                    if etype in _FATAL_ERRORS:
                        raise
                    error_str = str(exc)[:150]
                    if etype == "FloodWaitError":
                        seconds = getattr(exc, "seconds", 60)
                        flood_multiplier = min(flood_multiplier * 2, 8.0)
                        log.warning(
                            "activity_session %d FloodWait %ds, multiplier=%.1f",
                            sess_id, seconds, flood_multiplier,
                        )
                        await asyncio.sleep(min(seconds, 300))
                    success = False

                dur_s = time.monotonic() - t0

                try:
                    await pool.execute(
                        """INSERT INTO resource_activity_log
                           (session_id, account_id, action_type, resource_ref, success, error)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        sess_id, acc_id, action, ref, success, error_str,
                    )
                except Exception:
                    pass

                try:
                    from services import infra_memory
                    infra_memory.record_account_op(acc_id, "resource_activity", success, duration_s=dur_s)
                except Exception:
                    pass

                if success:
                    total_ok += 1
                else:
                    total_fail += 1

                if i < actions_per_acc - 1:
                    pause = base_delay * flood_multiplier
                    if (i + 1) % 4 == 0:
                        pause = max(pause * 2, 60.0)
                    await asyncio.sleep(pause + random.uniform(-5, 15))

        except Exception as exc:
            etype = type(exc).__name__
            log.warning("activity_session acc=%d: %s: %s", acc_id, etype, exc)
            if etype in _FATAL_ERRORS:
                try:
                    await pool.execute(
                        "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc_id
                    )
                except Exception:
                    pass
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception:
                pass

        await asyncio.sleep(random.uniform(20, 60))

    new_day    = current_d + 1
    completed  = new_day >= target_d
    new_status = "completed" if completed else "active"

    await pool.execute(
        """UPDATE resource_activity_sessions
           SET current_day=$1, last_run_at=NOW(), status=$2
           WHERE id=$3""",
        new_day, new_status, sess_id,
    )

    log.info(
        "activity_session %d day=%d/%d ok=%d fail=%d completed=%s",
        sess_id, new_day, target_d, total_ok, total_fail, completed,
    )
    return {
        "actions_done": total_ok + total_fail,
        "actions_ok":   total_ok,
        "actions_fail": total_fail,
        "completed":    completed,
    }


async def run_activity_loop(pool: asyncpg.Pool, interval_hours: int = 1) -> None:
    """Фоновый цикл: запускает активные сессии раз в сутки."""
    while True:
        try:
            rows = await pool.fetch(
                """SELECT * FROM resource_activity_sessions
                   WHERE status = 'active'
                     AND (last_run_at IS NULL
                          OR last_run_at < NOW() - INTERVAL '20 hours')""",
            )
            if rows:
                log.info("activity loop: found %d sessions to run", len(rows))
            for row in rows:
                await run_resource_activity_session(pool, dict(row))
                await asyncio.sleep(30)
        except Exception as e:
            log.warning("activity loop error: %s", e)
        await asyncio.sleep(interval_hours * 3600)
