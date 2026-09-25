"""Ghost Engine — autonomous continuous background presence for TG accounts.

Each enabled ghost_profile selects one random action per cycle based on
personality type.  Actions use only the account's existing subscriptions —
no new channel joins, no group posts.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot
from telethon.errors import (
    AuthKeyError,
    FloodWaitError,
    UserDeactivatedBanError,
    ChatWriteForbiddenError,
    PeerFloodError,
)
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.types import ReactionEmoji

from services.account_manager import _connect_and_track, _make_client
from services.logger import log_exc_swallow

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


_LOOP_INTERVAL = 200        # seconds between full sweeps
_STAGGER_MIN   = 4          # seconds between individual account actions
_STAGGER_MAX   = 18

PERSONALITY_CAPS: dict[str, int] = {
    "ghost":   8,
    "watcher": 15,
    "active":  25,
}

_SAFE_REACTIONS = ["👍", "❤️", "🔥", "👏", "🎉", "💯", "😮"]


def _ghost_skip_probability(governor_level: str, local_night: bool) -> float:
    """Вероятность ПРОПУСТИТЬ фоновое ghost-действие. Чистая, тестируемая.

    Ghost — расходуемый «шум», не операция: под давлением флота гасим его
    первым, а в локальную ночь аккаунт в основном «спит».
    """
    p = 0.0
    if governor_level == "red":
        p = 0.8
    elif governor_level == "amber":
        p = 0.4
    if local_night:
        p = max(p, 0.7)
    return p


async def _ghost_allowed(pool, owner_id, geo_country, now) -> bool:
    """Разрешить фоновое ghost-действие сейчас? Fail-open → True (не глушим зря).

    Только skip-гейт: НЕ создаёт новых подключений сессий. Уважает губернатор
    (кэш 45с, без нагрузки) и локальную ночь аккаунта (гео прокси)."""
    try:
        level = "green"
        if owner_id:
            from services import fleet_governor
            mult = await fleet_governor.tempo_multiplier(pool, owner_id)
            level = fleet_governor.level_for_multiplier(mult)
        try:
            from services import geo_tempo
            night = geo_tempo.is_local_night(geo_country, now)
        except Exception:
            night = False
        return random.random() >= _ghost_skip_probability(level, night)
    except Exception:
        return True


# ─── DB helpers ───────────────────────────────────────────────────────────────


async def _count_today_actions(pool: asyncpg.Pool, profile_id: int) -> int:
    row = await pool.fetchrow(
        """
        SELECT COUNT(*) AS cnt
        FROM ghost_action_log
        WHERE ghost_profile_id = $1
          AND executed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
        """,
        profile_id,
    )
    return int(row["cnt"]) if row else 0


async def _last_action_at(pool: asyncpg.Pool, profile_id: int) -> datetime | None:
    row = await pool.fetchrow(
        "SELECT MAX(executed_at) AS last FROM ghost_action_log WHERE ghost_profile_id = $1",
        profile_id,
    )
    v = row["last"] if row else None
    if v and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v


async def _log_action(
    pool: asyncpg.Pool,
    profile_id: int,
    account_id: int,
    action_type: str,
    target: str | None,
    result: str,
    error_msg: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO ghost_action_log
               (ghost_profile_id, account_id, action_type, target, result, error_msg)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        profile_id,
        account_id,
        action_type,
        target,
        result,
        error_msg,
    )


# ─── Actions ──────────────────────────────────────────────────────────────────


async def _act_update_status(client, pool, profile_id, account_id) -> None:
    await asyncio.wait_for(client(UpdateStatusRequest(offline=False)), timeout=_TG_TIMEOUT)
    await asyncio.sleep(random.uniform(4, 12))
    await asyncio.wait_for(client(UpdateStatusRequest(offline=True)), timeout=_TG_TIMEOUT)
    await _log_action(pool, profile_id, account_id, "update_status", None, "ok")


async def _act_read_dialogs(client, pool, profile_id, account_id) -> None:
    dialogs = await asyncio.wait_for(client.get_dialogs(limit=random.randint(5, 12)), timeout=_TG_TIMEOUT)
    targets = []
    sample = random.sample(dialogs, min(random.randint(1, 3), len(dialogs)))
    for d in sample:
        try:
            await asyncio.wait_for(client.send_read_acknowledge(d.entity), timeout=_TG_TIMEOUT)
            t = getattr(d.entity, "title", None) or getattr(d.entity, "username", None)
            if t:
                targets.append(t)
            await asyncio.sleep(random.uniform(1, 4))
        except Exception as e:
            log_exc_swallow(log, "_act_read_dialogs: mark_read")
    await _log_action(pool, profile_id, account_id, "read_dialogs", ",".join(targets[:3]) or None, "ok")


async def _act_react(client, pool, profile_id, account_id) -> None:
    dialogs = await asyncio.wait_for(client.get_dialogs(limit=25), timeout=_TG_TIMEOUT)
    channels = [
        d for d in dialogs
        if hasattr(d.entity, "broadcast") and d.entity.broadcast
    ]
    if not channels:
        await _log_action(pool, profile_id, account_id, "react", None, "skip", "no_channels")
        return
    ch = random.choice(channels[:15])
    msgs = await asyncio.wait_for(client.get_messages(ch.entity, limit=8), timeout=_TG_TIMEOUT)
    if not msgs:
        await _log_action(pool, profile_id, account_id, "react", None, "skip", "no_messages")
        return
    msg = random.choice(msgs)
    emoji = random.choice(_SAFE_REACTIONS)
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        await asyncio.wait_for(client(SendReactionRequest(
            peer=ch.entity,
            msg_id=msg.id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        )), timeout=_TG_TIMEOUT)
        target = getattr(ch.entity, "title", None) or getattr(ch.entity, "username", None) or "?"
        await _log_action(pool, profile_id, account_id, "react", target, "ok")
    except (ChatWriteForbiddenError, PeerFloodError) as e:
        await _log_action(pool, profile_id, account_id, "react", None, "skip", str(e)[:120])
    except Exception as e:
        await _log_action(pool, profile_id, account_id, "react", None, "error", str(e)[:200])


async def _act_forward_saved(client, pool, profile_id, account_id) -> None:
    dialogs = await asyncio.wait_for(client.get_dialogs(limit=25), timeout=_TG_TIMEOUT)
    channels = [
        d for d in dialogs
        if hasattr(d.entity, "broadcast") and d.entity.broadcast
    ]
    if not channels:
        await _log_action(pool, profile_id, account_id, "forward_saved", None, "skip", "no_channels")
        return
    ch = random.choice(channels[:15])
    msgs = await asyncio.wait_for(client.get_messages(ch.entity, limit=12), timeout=_TG_TIMEOUT)
    if not msgs:
        await _log_action(pool, profile_id, account_id, "forward_saved", None, "skip", "no_messages")
        return
    msg = random.choice(msgs)
    try:
        await asyncio.wait_for(client.forward_messages("me", msg.id, ch.entity), timeout=_TG_TIMEOUT)
        target = getattr(ch.entity, "title", None) or getattr(ch.entity, "username", None) or "?"
        await _log_action(pool, profile_id, account_id, "forward_saved", target, "ok")
    except Exception as e:
        await _log_action(pool, profile_id, account_id, "forward_saved", None, "error", str(e)[:200])


_ACTION_POOLS: dict[str, list[str]] = {
    "ghost":   ["update_status", "update_status", "read_dialogs"],
    "watcher": ["update_status", "read_dialogs", "read_dialogs", "react"],
    "active":  ["update_status", "read_dialogs", "react", "react", "forward_saved"],
}

_ACTION_FNS = {
    "update_status": _act_update_status,
    "read_dialogs":  _act_read_dialogs,
    "react":         _act_react,
    "forward_saved": _act_forward_saved,
}


# ─── Profile processing ───────────────────────────────────────────────────────


async def _process_profile(pool: asyncpg.Pool, profile: asyncpg.Record) -> None:
    profile_id = profile["id"]
    account_id = profile["account_id"]
    personality = profile["personality"]

    now = datetime.now(timezone.utc)
    hour = now.hour
    start = profile["active_hours_start"]
    end   = profile["active_hours_end"]
    if start <= end:
        in_window = start <= hour < end
    else:  # wraps midnight
        in_window = hour >= start or hour < end
    if not in_window:
        return

    last = await _last_action_at(pool, profile_id)
    if last:
        elapsed_min = (now - last).total_seconds() / 60
        if elapsed_min < profile["cooldown_minutes"]:
            return

    done_today = await _count_today_actions(pool, profile_id)
    if done_today >= profile["daily_cap"]:
        return

    acc = await pool.fetchrow(
        """
        SELECT a.id, a.owner_id, a.session_str, a.cooldown_until,
               a.device_model, a.system_version, a.app_version,
               a.lang_code, a.system_lang_code,
               p.proxy_url, p.geo_country
        FROM tg_accounts a
        LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
        WHERE a.id = $1 AND COALESCE(a.in_operation, FALSE) = FALSE AND COALESCE(a.acc_status,'active') NOT IN ('banned','deactivated','session_expired')
        """,
        account_id,
    )
    if not acc:
        return

    if acc["cooldown_until"] and acc["cooldown_until"].replace(tzinfo=timezone.utc) > now:
        return

    # Ghost — фоновый шум, а не операция: под давлением флота его гасим первым
    # (лишняя активность при рисках банов — плохой размен). И уважаем ЛОКАЛЬНУЮ
    # ночь аккаунта: глубокой ночью «человек» спит. Оба — additive skip-гейты,
    # новых подключений сессий не создают.
    if not await _ghost_allowed(pool, acc["owner_id"], acc["geo_country"], now):
        return

    session = acc["session_str"]
    if not session:
        return

    acc_dict = dict(acc)
    action = random.choice(_ACTION_POOLS.get(personality, _ACTION_POOLS["ghost"]))
    fn = _ACTION_FNS[action]

    # Атомарный захват аккаунта у единого арбитра op_worker ПЕРЕД открытием
    # сессии. SELECT выше фильтрует in_operation=FALSE по БД, но это снимок: пока
    # призрак дошёл до коннекта, операция могла захватить ту же сессию in-memory.
    # Если аккаунт уже занят — молча пропускаем цикл (фоновый шум подождёт), иначе
    # один auth-key коннектится призраком И операцией одновременно → Telegram
    # видит вход с двух мест и УНИЧТОЖАЕТ ключ (AUTH_KEY_DUPLICATED, безвозвратно).
    try:
        from services import op_worker as _opw
    except Exception:
        _opw = None
    leased = True
    if _opw is not None:
        try:
            leased = await _opw.try_claim_account(account_id)
        except Exception:
            leased = True  # op_worker недоступен — best-effort как раньше
    if not leased:
        log.debug("Ghost Engine: account %d занят операцией — пропуск цикла", account_id)
        return

    try:
        client = _make_client(session, acc_dict)
        # Коннект — через канонический помощник account_manager: у `async with client`
        # потолка нет вовсе (он зовёт client.start()), а аккаунт здесь уже захвачен
        # у арбитра op_worker — повисшее рукопожатие держало бы его занятым вечно.
        # Бонусом помощник пишет успех прокси в infra_memory, чего `async with` не делал.
        await _connect_and_track(client, acc_dict, "ghost")
        try:
            await fn(client, pool, profile_id, account_id)
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.debug("Ghost Engine: disconnect failed")
    except FloodWaitError as e:
        # Пропустить цикл мало: без записи в пульс аккаунт остаётся спокойным
        # для операций, разбора аудитории и рассылки.
        from services import flood_engine as _fe

        await _fe.note_flood(pool, account_id, e, "ghost")
        await _log_action(pool, profile_id, account_id, action, None, "skip", f"flood:{e.seconds}s")
    except (UserDeactivatedBanError, AuthKeyError) as e:
        log.warning("Ghost Engine: account %d banned/invalid, disabling profile %d", account_id, profile_id)
        await pool.execute(
            "UPDATE ghost_profiles SET enabled = FALSE, updated_at = NOW() WHERE id = $1",
            profile_id,
        )
        await _log_action(pool, profile_id, account_id, action, None, "error", str(e)[:120])
    except Exception as e:
        log.debug("Ghost Engine: profile %d / account %d error: %s", profile_id, account_id, e)
        await _log_action(pool, profile_id, account_id, action, None, "error", str(e)[:200])
    finally:
        # Всегда отпускаем аккаунт назад арбитру — иначе он «зомби» до реконсилера.
        if _opw is not None and leased:
            try:
                await _opw.release_accounts([account_id])
            except Exception:
                log.debug("Ghost Engine: release_accounts failed for %d", account_id)


# ─── Main loop ────────────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    log.info("Ghost Engine started")
    while True:
        try:
            profiles = await pool.fetch(
                "SELECT * FROM ghost_profiles WHERE enabled = TRUE ORDER BY id"
            )
            for profile in profiles:
                try:
                    await _process_profile(pool, profile)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("Ghost Engine: unhandled error in profile %d: %s", profile["id"], e)
                await asyncio.sleep(random.uniform(_STAGGER_MIN, _STAGGER_MAX))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Ghost Engine loop error: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL)
