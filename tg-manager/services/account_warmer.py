"""
Account Warming System — постепенный разогрев новых аккаунтов.

Имитирует натуральное поведение:
- День 1-3: чтение сообщений, просмотр профилей
- День 4-7: лайки/реакции, вступление в каналы
- День 8-14: комментарии, групповые сообщения
- День 15+: полная активность

Все действия логируются в account_warmup_log.
Статус плана хранится в account_warmup_plans.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

import asyncpg
from database import db
from services.logger import log_exc_swallow
from services import infra_memory

log = logging.getLogger(__name__)

# Публичные каналы/группы для "прогрева" (вступление, чтение)
# Используем только проверенные публичные каналы
_WARMUP_PUBLIC_CHANNELS = [
    "@telegram",
    "@durov",
    "@tginfo",
    "@bbcrussian",
    "@rian_ru",
    "@rbc_news",
    "@lentach",
    "@meduzaio",
    "@breakingmash",
    "@varlamov",
    "@proglib",
    "@linuxoid",
    "@reuters",
    "@bbc",
    "@guardian",
    "@techcrunch",
    "@hackernoon",
    "@wired",
    "@spacex",
    "@nasa",
    "@nature",
]

# Нишевые каналы для специализированного прогрева (по категориям)
# 7 ниш с достаточным кол-вом каналов для разнообразия + fallback на общие
_NICHE_CHANNELS: dict[str, list[str]] = {
    "tech": [
        "@proglib",
        "@techcrunch",
        "@hackernoon",
        "@wired",
        "@linuxoid",
        "@telegram",
        "@bbcrussian",
        "@rian_ru",
        "@rbc_news",
    ],
    "news": [
        "@rian_ru",
        "@rbc_news",
        "@lentach",
        "@meduzaio",
        "@bbcrussian",
        "@breakingmash",
        "@varlamov",
        "@reuters",
        "@bbc",
        "@guardian",
    ],
    "crypto": [
        "@rbc_news",
        "@bbcrussian",
        "@breakingmash",
        "@rian_ru",
        "@proglib",
        "@hackernoon",
        "@linuxoid",
        "@techcrunch",
        "@wired",
        "@telegram",
        "@durov",
    ],
    "trading": [
        "@rbc_news",
        "@bbcrussian",
        "@rian_ru",
        "@reuters",
        "@bbc",
        "@guardian",
        "@varlamov",
        "@lentach",
        "@proglib",
        "@telegram",
    ],
    "marketing": [
        "@proglib",
        "@hackernoon",
        "@rbc_news",
        "@bbcrussian",
        "@varlamov",
        "@lentach",
        "@techcrunch",
        "@wired",
        "@telegram",
        "@rian_ru",
    ],
    "business": [
        "@rbc_news",
        "@bbcrussian",
        "@rian_ru",
        "@reuters",
        "@bbc",
        "@guardian",
        "@lentach",
        "@varlamov",
        "@proglib",
        "@telegram",
    ],
    "entertainment": [
        "@varlamov",
        "@lentach",
        "@breakingmash",
        "@meduzaio",
        "@bbcrussian",
        "@bbc",
        "@telegram",
        "@rian_ru",
        "@proglib",
        "@durov",
    ],
    "science": [
        "@spacex",
        "@nasa",
        "@nature",
        "@wired",
        "@guardian",
        "@proglib",
        "@hackernoon",
    ],
    "sports": ["@telegram", "@bbcrussian", "@rian_ru", "@lentach", "@varlamov"],
    "general": _WARMUP_PUBLIC_CHANNELS,
}

_WARMUP_SEARCH_QUERIES = [
    "новости",
    "технологии",
    "бизнес",
    "криптовалюта",
    "спорт",
    "кино",
    "музыка",
    "путешествия",
    "инвестиции",
    "мемы",
    "еда",
    "здоровье",
    "python",
    "android",
    "telegram",
    "gaming",
    "ai",
    "программирование",
    "стартап",
    "маркетинг",
]

_WARMUP_REACTIONS = ["👍", "❤️", "🔥", "🎉", "👏", "😍", "💯", "🤩", "😂", "🏆"]
_WARMUP_BOTS = ["@BotFather", "@Stickers", "@gamee"]

_COMMENT_TEXTS = [
    "👍",
    "Спасибо!",
    "Интересно",
    "Согласен",
    "Хорошая тема",
    "Полезная информация",
    "Да, именно",
    "Отличный материал!",
    "Актуально",
    "👏",
    "Интересная точка зрения",
    "Благодарю за пост",
    "Продолжайте",
    "Поддерживаю",
    "Спасибо за контент",
    "Очень полезно",
    "🔥",
    "Хороший контент",
    "Важная тема",
]

_BOT_COMMANDS = ["/start", "/help", "/menu", "/info"]

# Действия по дням разогрева — прогрессивная нагрузка
_WARMUP_SCHEDULE: dict[str, list[str]] = {
    "days_1_3": [
        "read_channel",
        "view_profile",
        "open_chat",
        "mark_read",
        "update_presence",
        "browse_dialogs",
        "check_notifications",
    ],
    "days_4_7": [
        "read_channel",
        "view_profile",
        "join_channel",
        "send_reaction",
        "mark_read",
        "browse_dialogs",
        "forward_to_saved",
        "story_view",
        "check_notifications",
    ],
    "days_8_14": [
        "read_channel",
        "join_channel",
        "send_reaction",
        "search",
        "forward_to_saved",
        "vote_poll",
        "own_channel_read",
        "mark_read",
        "story_view",
    ],
    "days_15_plus": [
        "read_channel",
        "join_channel",
        "send_reaction",
        "search",
        "dm_bot",
        "send_comment",
        "smart_bot_start",
        "smart_bot_help",
        "forward_to_saved",
        "vote_poll",
        "own_channel_read",
        "own_bot_start",
        "story_view",
    ],
}

# Профильные веса: определяют «характер» аккаунта при выборе действий
_PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "reader": {
        "read_channel": 0.40,
        "mark_read": 0.25,
        "browse_dialogs": 0.15,
        "view_profile": 0.12,
        "send_reaction": 0.08,
    },
    "commenter": {
        "read_channel": 0.25,
        "send_comment": 0.30,
        "send_reaction": 0.20,
        "mark_read": 0.15,
        "vote_poll": 0.10,
    },
    "reactor": {
        "send_reaction": 0.45,
        "read_channel": 0.30,
        "forward_to_saved": 0.15,
        "vote_poll": 0.10,
    },
    "lurker": {
        "browse_dialogs": 0.40,
        "mark_read": 0.35,
        "read_channel": 0.15,
        "check_notifications": 0.10,
    },
    "mixed": {
        "read_channel": 0.25,
        "send_reaction": 0.20,
        "mark_read": 0.20,
        "browse_dialogs": 0.15,
        "view_profile": 0.10,
        "forward_to_saved": 0.07,
        "vote_poll": 0.03,
    },
}


def _profile_weighted_action(profile: str, day_actions: list[str]) -> str:
    """Выбирает действие с учётом профиля аккаунта.

    Смещает вероятности в соответствии с профилем, исключает недоступные
    для текущего дня действия. Fallback на random.choice при нулевых весах.
    """
    weights = _PROFILE_WEIGHTS.get(profile, _PROFILE_WEIGHTS["mixed"])
    eligible = {a: weights.get(a, 0.03) for a in day_actions}
    total = sum(eligible.values())
    if total == 0:
        return random.choice(day_actions)
    return random.choices(
        list(eligible.keys()),
        weights=[v / total for v in eligible.values()],
        k=1,
    )[0]


@dataclass
class WarmupPlan:
    plan_id: int
    account_id: int
    owner_id: int
    current_day: int
    target_days: int
    daily_actions: int
    status: str


def _compute_warmup_level(actions_done: int) -> str:
    """Определяет уровень прогрева по количеству выполненных действий за сессию."""
    if actions_done >= 6:
        return "deep"
    if actions_done >= 3:
        return "medium"
    return "light"


async def get_account_niche_channels(pool: asyncpg.Pool, account_id: int) -> list[str]:
    """Возвращает список каналов для прогрева с учётом нишевого профиля аккаунта."""
    try:
        row = await pool.fetchrow(
            "SELECT niche, custom_channels FROM account_niche_profiles WHERE account_id=$1",
            account_id,
        )
        if row:
            custom: list = row["custom_channels"] or []
            if custom:
                return custom
            niche = row["niche"] or "general"
            return _NICHE_CHANNELS.get(niche, _WARMUP_PUBLIC_CHANNELS)
    except Exception as e:
        log.debug("warmup get_niche_channels acc=%d: %s", account_id, e)
    return _WARMUP_PUBLIC_CHANNELS


async def create_warmup_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    account_id: int,
    plan_type: str = "standard",  # standard / gentle / aggressive
) -> int:
    """Создаёт план разогрева для аккаунта. Возвращает plan_id.

    daily_actions здесь — это ПОТОЛОК (target) на финальных днях. Реальное число
    действий в день рассчитывается через _actions_for_day_count() с рампой
    low→medium→high, поэтому свежий аккаунт никогда не получает максимум сразу.
    """
    # Note: "aggressive" capped at 12/day (was 20) — 20 actions/day on a fresh
    # low-trust account is the #1 ban trigger. target_days lengthened accordingly.
    daily_map = {"gentle": 5, "standard": 10, "aggressive": 12}
    days_map = {"gentle": 21, "standard": 14, "aggressive": 10}

    row = await pool.fetchrow(
        """INSERT INTO account_warmup_plans(
               owner_id, account_id, plan_type, daily_actions, target_days
           ) VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (account_id) DO UPDATE
               SET status='active', current_day=0, started_at=NOW(),
                   plan_type=$3, daily_actions=$4, target_days=$5
           RETURNING id""",
        owner_id,
        account_id,
        plan_type,
        daily_map.get(plan_type, 5),
        days_map.get(plan_type, 14),
    )
    log.info("warmup: created plan %d for acc=%d", row["id"], account_id)
    return row["id"]


async def get_active_plans(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT wp.*, a.phone, a.first_name, a.trust_score
           FROM account_warmup_plans wp
           JOIN tg_accounts a ON a.id = wp.account_id
           WHERE wp.owner_id=$1 AND wp.status='active'
           ORDER BY wp.started_at""",
        owner_id,
    )
    return [dict(r) for r in rows]


def _get_actions_for_day(day: int) -> list[str]:
    if day <= 3:
        return _WARMUP_SCHEDULE["days_1_3"]
    if day <= 7:
        return _WARMUP_SCHEDULE["days_4_7"]
    if day <= 14:
        return _WARMUP_SCHEDULE["days_8_14"]
    return _WARMUP_SCHEDULE["days_15_plus"]


_FATAL_ERRORS = frozenset(
    {
        "UserDeactivatedBanError",
        "UserDeactivatedError",
        "AuthKeyUnregisteredError",
        "PhoneNumberBannedError",
        "SessionRevokedError",
        "SessionExpiredError",
    }
)

# Restriction signals: account is rate-limited/spam-flagged → must STOP warming it,
# not keep hammering. Distinct from fatal (ban) — account is alive but throttled.
_RESTRICTION_ERRORS = frozenset(
    {
        "PeerFloodError",
        "UserRestrictedError",
    }
)


def _is_fatal_error(etype: str, error_text: str = "") -> bool:
    if etype in _FATAL_ERRORS:
        return True
    low = (error_text or "").lower()
    return any(
        m in low
        for m in (
            "auth_key_unregistered",
            "session_revoked",
            "session_expired",
            "user_deactivated",
            "phone_number_banned",
            "key is not registered",
            "registered in the system",
        )
    )


def _is_restriction_error(etype: str, error_text: str = "") -> bool:
    if etype in _RESTRICTION_ERRORS:
        return True
    low = (error_text or "").lower()
    return "peer_flood" in low or "spam" in low or "too many requests" in low


# FloodWait longer than this (seconds) is treated as a stop-the-session signal:
# we pause the plan rather than blocking a task for hours. Telegram escalates if
# you keep issuing calls while a long flood-wait is active.
_MAX_FLOOD_WAIT_INLINE = 1800  # 30 min


def _actions_for_day_count(day: int, target_daily: int) -> int:
    """Ramp action volume low→medium→high so fresh accounts are never hit at max.

    A brand-new (day 0) account does only a few actions; volume rises with age.
    This is the single most important ban-avoidance control for warmup.
    """
    if day <= 1:
        return max(2, target_daily // 4)
    if day <= 4:
        return max(3, target_daily // 2)
    if day <= 9:
        return max(4, (target_daily * 3) // 4)
    return target_daily

# In-memory guards: предотвращают одновременный запуск прогрева одного и того же плана/сессии
_active_plan_ids: set[int] = set()
_active_session_ids: set[int] = set()
_plan_locks: dict[int, asyncio.Lock] = {}  # Per-plan locks for fine-grained concurrency
_session_locks: dict[int, asyncio.Lock] = {}  # Per-session locks
_global_lock = asyncio.Lock()  # For accessing _plan_locks/_session_locks dicts


async def _get_plan_lock(plan_id: int) -> asyncio.Lock:
    """Get or create a lock for a specific plan."""
    async with _global_lock:
        if plan_id not in _plan_locks:
            _plan_locks[plan_id] = asyncio.Lock()
        return _plan_locks[plan_id]


async def _get_session_lock(session_id: int) -> asyncio.Lock:
    """Get or create a lock for a specific session."""
    async with _global_lock:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
        return _session_locks[session_id]


async def _perform_read_channel(client, channel_ref: str) -> bool:
    """Читаем канал: получаем последние 10-15 сообщений, имитируем скролл."""
    try:
        entity = await client.get_entity(channel_ref)
        limit = random.randint(10, 15)
        msgs = await client.get_messages(entity, limit=limit)
        if not msgs:
            return False
        # Имитируем поочерёдное "чтение" каждого сообщения
        for _ in msgs:
            await asyncio.sleep(random.uniform(0.8, 2.5))
        await asyncio.sleep(random.uniform(2, 5))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup read_channel %s", channel_ref)
        return False


async def _perform_view_profile(client, channel_ref: str) -> bool:
    """Открываем профиль/инфо канала."""
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest

        entity = await client.get_entity(channel_ref)
        await client(GetFullChannelRequest(entity))
        await asyncio.sleep(random.uniform(3, 8))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup view_profile %s", channel_ref)
        return False


async def _perform_open_chat(client, channel_ref: str) -> bool:
    """Открываем чат и просматриваем сообщения (симуляция скролла)."""
    try:
        entity = await client.get_entity(channel_ref)
        count = random.randint(8, 20)
        async for _msg in client.iter_messages(entity, limit=count):
            await asyncio.sleep(random.uniform(0.5, 1.8))
        await asyncio.sleep(random.uniform(1, 4))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup open_chat %s", channel_ref)
        return False


async def _perform_send_reaction(client, channel_ref: str) -> bool:
    """Ставим реакцию на случайное сообщение в канале."""
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        entity = await client.get_entity(channel_ref)
        msgs = await client.get_messages(entity, limit=10)
        if not msgs:
            return False
        msg = random.choice(list(msgs))
        emoticon = random.choice(_WARMUP_REACTIONS)
        await client(
            SendReactionRequest(
                peer=entity,
                msg_id=msg.id,
                reaction=[ReactionEmoji(emoticon=emoticon)],
            )
        )
        await asyncio.sleep(random.uniform(1, 4))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup send_reaction %s", channel_ref)
        return False


async def _perform_dm_bot(client) -> bool:
    """Открываем официального бота и отправляем /start."""
    try:
        bot_handle = random.choice(_WARMUP_BOTS)
        entity = await client.get_entity(bot_handle)
        await client.send_message(entity, "/start")
        await asyncio.sleep(random.uniform(5, 15))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup dm_bot")
        return False


async def _perform_join_channel(client, channel_ref: str) -> bool:
    """Вступаем в публичный канал."""
    try:
        from telethon.tl.functions.channels import JoinChannelRequest

        entity = await client.get_entity(channel_ref)
        await client(JoinChannelRequest(entity))
        await asyncio.sleep(random.uniform(3, 8))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        if etype == "FloodWaitError":
            seconds = getattr(e, "seconds", 60)
            log.warning(
                "warmup join_channel FloodWait %ds for %s", seconds, channel_ref
            )
            await asyncio.sleep(min(seconds, 300))
            raise
        log_exc_swallow(log, "warmup join_channel %s", channel_ref)
        return False


async def _perform_search(client, query: str) -> bool:
    """Поиск в Telegram."""
    try:
        from telethon.tl.functions.contacts import SearchRequest

        await client(SearchRequest(q=query, limit=5))
        await asyncio.sleep(random.uniform(3, 7))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup search %s", query)
        return False


async def _perform_mark_read(client, channel_ref: str) -> bool:
    """ReadHistoryRequest — реально отмечает сообщения прочитанными."""
    try:
        from telethon.tl.functions.messages import ReadHistoryRequest

        entity = await client.get_entity(channel_ref)
        msgs = await client.get_messages(entity, limit=5)
        if msgs:
            await client(ReadHistoryRequest(peer=entity, max_id=msgs[0].id))
        await asyncio.sleep(random.uniform(2, 5))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup mark_read %s", channel_ref)
        return False


async def _perform_update_presence(client) -> bool:
    """UpdateStatusRequest — симулируем онлайн-присутствие.

    offline=True всегда выставляется в finally: иначе при ошибке между
    online и offline аккаунт остаётся «вечно онлайн» — антифингерпринт-сигнал.
    """
    from telethon.tl.functions.account import UpdateStatusRequest

    went_online = False
    try:
        await client(UpdateStatusRequest(offline=False))
        went_online = True
        await asyncio.sleep(random.uniform(10, 30))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup update_presence")
        return False
    finally:
        if went_online:
            try:
                await client(UpdateStatusRequest(offline=True))
            except Exception:
                log_exc_swallow(log, "warmup update_presence offline reset")


async def _perform_browse_dialogs(client) -> bool:
    """GetDialogs — симуляция открытия списка диалогов."""
    try:
        dialogs = await client.get_dialogs(limit=random.randint(10, 20))
        for _ in dialogs[: random.randint(3, 7)]:
            await asyncio.sleep(random.uniform(0.5, 1.5))
        await asyncio.sleep(random.uniform(3, 8))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup browse_dialogs")
        return False


async def _perform_forward_to_saved(client, channel_ref: str) -> bool:
    """Пересылаем интересный пост в Saved Messages."""
    try:
        entity = await client.get_entity(channel_ref)
        msgs = await client.get_messages(entity, limit=20)
        if not msgs:
            return False
        candidates = [m for m in msgs if m.media or (m.text and len(m.text or "") > 50)]
        msg = random.choice(candidates if candidates else list(msgs))
        await client.forward_messages("me", msg)
        await asyncio.sleep(random.uniform(2, 6))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup forward_to_saved %s", channel_ref)
        return False


async def _perform_vote_poll(client, channel_ref: str) -> bool:
    """Голосуем в опросе если есть в канале."""
    try:
        from telethon.tl.functions.messages import SendVoteRequest
        from telethon.tl.types import MessageMediaPoll

        entity = await client.get_entity(channel_ref)
        msgs = await client.get_messages(entity, limit=30)
        poll_msgs = [
            m
            for m in msgs
            if isinstance(m.media, MessageMediaPoll)
            and not m.media.poll.closed
            and not (m.media.results and m.media.results.min)
        ]
        if not poll_msgs:
            return False
        msg = random.choice(poll_msgs)
        options = msg.media.poll.answers
        if not options:
            return False
        chosen = random.choice(options)
        await client(
            SendVoteRequest(peer=entity, msg_id=msg.id, options=[chosen.option])
        )
        await asyncio.sleep(random.uniform(2, 5))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup vote_poll %s", channel_ref)
        return False


async def _perform_send_comment(client, channel_ref: str) -> bool:
    """Отправляем комментарий к посту через группу обсуждений."""
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest

        entity = await client.get_entity(channel_ref)
        if not hasattr(entity, "broadcast") or not entity.broadcast:
            return False  # only channels have discussions
        full = await client(GetFullChannelRequest(entity))
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if not linked_id:
            return False
        msgs = await client.get_messages(entity, limit=20)
        msg_with_replies = [m for m in msgs if m.replies and m.replies.replies > 0]
        if not msg_with_replies:
            return False
        post = random.choice(msg_with_replies[:5])
        comment = random.choice(_COMMENT_TEXTS)
        discussion = await client.get_entity(linked_id)
        await client.send_message(discussion, comment, comment_to=post.id)
        await asyncio.sleep(random.uniform(5, 15))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup send_comment %s", channel_ref)
        return False


async def _perform_smart_bot_cmd(client, bot_ref: str, command: str = "/start") -> bool:
    """Отправляем команду боту, читаем ответ — умная имитация пользователя."""
    try:
        entity = await client.get_entity(bot_ref)
        await client.send_message(entity, command)
        await asyncio.sleep(random.uniform(3, 10))
        # Читаем ответ бота
        await client.get_messages(entity, limit=3)
        await asyncio.sleep(random.uniform(2, 5))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup smart_bot_cmd %s %s", bot_ref, command)
        return False


async def _perform_own_channel_read(client, channel_ref: str) -> bool:
    """Читаем и реагируем на пост в своём канале (имитация органического просмотра)."""
    try:
        from telethon.tl.functions.messages import (
            ReadHistoryRequest,
            SendReactionRequest,
        )
        from telethon.tl.types import ReactionEmoji

        entity = await client.get_entity(channel_ref)
        msgs = await client.get_messages(entity, limit=10)
        if not msgs:
            return False
        # Mark as read
        await client(ReadHistoryRequest(peer=entity, max_id=msgs[0].id))
        await asyncio.sleep(random.uniform(3, 8))
        # With 40% chance add reaction
        if random.random() < 0.4:
            msg = random.choice(list(msgs))
            emoticon = random.choice(_WARMUP_REACTIONS)
            try:
                await client(
                    SendReactionRequest(
                        peer=entity,
                        msg_id=msg.id,
                        reaction=[ReactionEmoji(emoticon=emoticon)],
                    )
                )
                await asyncio.sleep(random.uniform(1, 3))
            except Exception:
                pass
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup own_channel_read %s", channel_ref)
        return False


async def _perform_story_view(client) -> bool:
    """Просматривает доступные истории контактов (Stories)."""
    try:
        try:
            from telethon.tl.functions.stories import GetAllStoriesRequest

            await client(GetAllStoriesRequest(next=False, hidden=False))
            await asyncio.sleep(random.uniform(4, 12))
        except (ImportError, AttributeError):
            # Stories API not available on this client version - skip gracefully
            log.debug("warmup story_view: API not available, skipping")
            return True  # Not a failure, just not supported
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup story_view")
        return False


async def _perform_check_notifications(client) -> bool:
    """Симулирует проверку уведомлений (GetState)."""
    try:
        from telethon.tl.functions.updates import GetStateRequest

        await client(GetStateRequest())
        await asyncio.sleep(random.uniform(2, 6))
        return True
    except Exception as e:
        etype = type(e).__name__
        if etype in _FATAL_ERRORS:
            raise
        log_exc_swallow(log, "warmup check_notifications")
        return False


async def _log_warmup_action(
    pool: asyncpg.Pool,
    account_id: int,
    action_type: str,
    target: str,
    success: bool,
    error: str | None = None,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO account_warmup_log(account_id, action_type, target, success, error)
               VALUES ($1,$2,$3,$4,$5)""",
            account_id,
            action_type,
            target,
            success,
            error,
        )
    except Exception as e:
        log.debug("warmup log write: %s", e)


async def _get_warmup_resources(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Получает каналы и боты пользователя для прогрева собственных ресурсов."""
    try:
        bots = await pool.fetch(
            """SELECT DISTINCT username FROM managed_bots
               WHERE added_by=$1 AND is_active=TRUE AND username IS NOT NULL AND username != ''
               LIMIT 5""",
            owner_id,
        )
        channels = await pool.fetch(
            """SELECT DISTINCT channel_id, username, title FROM managed_channels
               WHERE owner_id=$1 AND username IS NOT NULL AND username != ''
               LIMIT 10""",
            owner_id,
        )
        return {
            "bots": [dict(r) for r in bots],
            "channels": [dict(r) for r in channels],
        }
    except Exception as e:
        log.debug("warmup get_resources owner=%d: %s", owner_id, e)
        return {"bots": [], "channels": []}


async def run_daily_warmup(
    pool: asyncpg.Pool,
    plan: dict,
    update_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """
    Выполняет дневные действия для одного плана разогрева.

    Аргументы:
        pool: пул подключений к БД
        plan: словарь с данными плана
        update_callback: опциональный коллбэк вида (step, total, description) ->
            None, вызывается после каждого действия для отслеживания прогресса.

    Возвращает {'actions_done', 'actions_ok', 'actions_fail', 'completed',
                'warmup_level'}.
    """
    account_id = plan["account_id"]
    owner_id = plan["owner_id"]
    current_day = plan["current_day"]
    daily_actions = plan["daily_actions"]
    plan_id = plan["id"]

    # Защита от параллельного запуска одного и того же плана
    plan_lock = await _get_plan_lock(plan_id)
    if plan_lock.locked():
        log.info("warmup: plan %d already running, skipping concurrent launch", plan_id)
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
            "warmup_level": "light",
        }
    async with plan_lock:
        return await _run_daily_warmup_impl(
            pool,
            plan,
            account_id,
            owner_id,
            current_day,
            daily_actions,
            plan_id,
            update_callback,
        )


async def _run_daily_warmup_impl(
    pool: asyncpg.Pool,
    plan: dict,
    account_id: int,
    owner_id: int,
    current_day: int,
    daily_actions: int,
    plan_id: int,
    update_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Внутренняя реализация run_daily_warmup — вызывается только через guard."""
    from services import account_manager

    # Получаем сессию аккаунта
    acc_row = await db.get_account_for_telethon(pool, account_id)
    if not acc_row:
        log.warning("warmup: account %d not found or inactive", account_id)
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
            "warmup_level": "light",
        }

    if not acc_row["session_str"]:
        log.warning("warmup: account %d has no session_str, skipping", account_id)
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
            "warmup_level": "light",
        }

    _skip_result = {
        "actions_done": 0,
        "actions_ok": 0,
        "actions_fail": 0,
        "completed": False,
        "warmup_level": "light",
    }

    # Не запускать прогрев на аккаунте, занятом активной операцией (op_worker).
    # Параллельное использование одной сессии двумя клиентами — прямой путь к бану.
    try:
        from services import op_worker as _opw

        if _opw.is_account_in_use(account_id):
            log.info(
                "warmup: acc=%d занят активной операцией — пропуск цикла", account_id
            )
            return _skip_result
    except Exception:
        log_exc_swallow(log, "warmup: is_account_in_use check failed")

    # Health/freshness gate: не разгоняем забаненные/неактивные аккаунты,
    # а очень свежие (< 24ч) держим на минимальной интенсивности (день 0).
    acc_health = await pool.fetchrow(
        "SELECT is_active, acc_status, trust_score, added_at FROM tg_accounts WHERE id=$1",
        account_id,
    )
    if acc_health:
        if acc_health["is_active"] is False:
            log.info("warmup: acc=%d неактивен — пропуск", account_id)
            return _skip_result
        if (acc_health["acc_status"] or "active") in (
            "banned",
            "spamblock",
            "deactivated",
        ):
            log.info(
                "warmup: acc=%d статус=%s — пропуск разогрева",
                account_id,
                acc_health["acc_status"],
            )
            return _skip_result
        # Очень свежий аккаунт: форсируем поведение дня 0 (минимум действий, read-only)
        added_at = acc_health["added_at"]
        if added_at is not None:
            try:
                from datetime import datetime, timezone

                age_h = (
                    datetime.now(timezone.utc) - added_at.replace(tzinfo=timezone.utc)
                ).total_seconds() / 3600.0
                if age_h < 24 and current_day > 0:
                    log.info(
                        "warmup: acc=%d возраст %.1fч < 24ч — день 0 интенсивность",
                        account_id,
                        age_h,
                    )
                    current_day = 0
            except (TypeError, ValueError, AttributeError):
                pass

    # Claim the account so op_worker/parallel warmup won't touch the same session.
    try:
        from services import op_worker as _opw

        await _opw.mark_accounts_in_use([account_id])
    except Exception:
        log_exc_swallow(log, "warmup: mark_accounts_in_use failed")

    # Профиль аккаунта из таблицы niche_profiles (если есть)
    niche_row = await pool.fetchrow(
        "SELECT niche, profile_type FROM account_niche_profiles WHERE account_id=$1",
        account_id,
    )
    acc_profile = (niche_row["profile_type"] if niche_row else None) or "mixed"

    device = dict(acc_row) if acc_row["device_model"] else None
    client = account_manager._make_client(acc_row["session_str"], device)

    actions_ok = 0
    actions_fail = 0
    consecutive_fails = 0
    available_actions = _get_actions_for_day(current_day)
    resources = await _get_warmup_resources(pool, owner_id)
    own_bots = resources["bots"]
    own_channels = resources["channels"]
    # Нишево-осведомлённый список каналов: через account_niche_profiles
    channels = await get_account_niche_channels(pool, account_id)
    if not channels:
        channels = _WARMUP_PUBLIC_CHANNELS.copy()
    random.shuffle(channels)

    # Описания действий для прогресс-коллбэка
    _action_descriptions = {
        "read_channel": "📖 читаю канал",
        "join_channel": "🔔 вступаю в канал",
        "send_reaction": "❤️ реакция на пост",
        "search": "🔍 поиск",
        "view_profile": "👁 смотрю профиль",
        "open_chat": "💬 открываю чат",
        "dm_bot": "🤖 пишу боту",
        "mark_read": "✅ отмечаю прочитанным",
        "update_presence": "🟢 онлайн-присутствие",
        "browse_dialogs": "📱 проверяю диалоги",
        "forward_to_saved": "📌 сохраняю пост",
        "vote_poll": "📊 голосую в опросе",
        "send_comment": "💬 оставляю комментарий",
        "own_channel_read": "📡 читаю свой канал",
        "smart_bot_start": "🤖 /start своему боту",
        "smart_bot_help": "🤖 /help своему боту",
        "own_bot_start": "🤖 запуск своего бота",
        "story_view": "📸 просматриваю истории",
        "check_notifications": "🔔 проверяю уведомления",
    }

    # Рампа объёма действий: свежий аккаунт (день 0-1) делает мало действий,
    # объём растёт с возрастом. Это ключевая защита от бана.
    day_actions_n = _actions_for_day_count(current_day, daily_actions)

    try:
        await asyncio.wait_for(client.connect(), timeout=15)

        for i in range(day_actions_n):
            # Профильно-взвешенный выбор действия
            action = _profile_weighted_action(acc_profile, available_actions)
            target = channels[i % len(channels)]
            success = False
            error = None
            t0_action = time.monotonic()

            try:
                if action in ("update_presence", "browse_dialogs"):
                    target = "self"
                    success = False
                    if action == "update_presence":
                        success = await asyncio.wait_for(
                            _perform_update_presence(client), timeout=60
                        )
                    else:
                        success = await asyncio.wait_for(
                            _perform_browse_dialogs(client), timeout=60
                        )

                elif action == "mark_read":
                    target = channels[i % len(channels)]
                    success = await asyncio.wait_for(
                        _perform_mark_read(client, target), timeout=60
                    )

                elif action == "forward_to_saved":
                    target = channels[i % len(channels)]
                    success = await asyncio.wait_for(
                        _perform_forward_to_saved(client, target), timeout=60
                    )

                elif action == "vote_poll":
                    target = channels[i % len(channels)]
                    success = await asyncio.wait_for(
                        _perform_vote_poll(client, target), timeout=60
                    )

                elif action == "send_comment":
                    target = channels[i % len(channels)]
                    success = await asyncio.wait_for(
                        _perform_send_comment(client, target), timeout=90
                    )

                elif action == "own_channel_read":
                    if own_channels:
                        ch = random.choice(own_channels)
                        target = (
                            f"@{ch['username']}"
                            if ch.get("username")
                            else str(ch["channel_id"])
                        )
                    else:
                        target = channels[i % len(channels)]
                    success = await asyncio.wait_for(
                        _perform_own_channel_read(client, target), timeout=60
                    )

                elif action in ("smart_bot_start", "own_bot_start"):
                    if own_bots:
                        bot = random.choice(own_bots)
                        target = f"@{bot['username']}"
                    else:
                        target = random.choice(_WARMUP_BOTS)
                    success = await asyncio.wait_for(
                        _perform_smart_bot_cmd(client, target, "/start"), timeout=60
                    )

                elif action == "smart_bot_help":
                    if own_bots:
                        bot = random.choice(own_bots)
                        target = f"@{bot['username']}"
                    else:
                        target = random.choice(_WARMUP_BOTS)
                    success = await asyncio.wait_for(
                        _perform_smart_bot_cmd(client, target, "/help"), timeout=60
                    )

                elif action == "read_channel":
                    success = await asyncio.wait_for(
                        _perform_read_channel(client, target), timeout=60
                    )
                elif action == "join_channel":
                    success = await asyncio.wait_for(
                        _perform_join_channel(client, target), timeout=60
                    )
                elif action == "search":
                    query = random.choice(_WARMUP_SEARCH_QUERIES)
                    success = await asyncio.wait_for(
                        _perform_search(client, query), timeout=60
                    )
                    target = f"search:{query}"
                elif action == "view_profile":
                    success = await asyncio.wait_for(
                        _perform_view_profile(client, target), timeout=60
                    )
                elif action == "open_chat":
                    success = await asyncio.wait_for(
                        _perform_open_chat(client, target), timeout=60
                    )
                elif action == "send_reaction":
                    success = await asyncio.wait_for(
                        _perform_send_reaction(client, target), timeout=60
                    )
                elif action == "dm_bot":
                    success = await asyncio.wait_for(
                        _perform_dm_bot(client), timeout=60
                    )
                    target = "dm_bot"
                elif action == "story_view":
                    target = "stories"
                    success = await asyncio.wait_for(
                        _perform_story_view(client), timeout=30
                    )
                elif action == "check_notifications":
                    target = "notifications"
                    success = await asyncio.wait_for(
                        _perform_check_notifications(client), timeout=20
                    )
                else:
                    await asyncio.sleep(random.uniform(2, 7))
                    success = True
            except asyncio.TimeoutError:
                error = "timeout"
                success = False
                log.warning(
                    "warmup: action %s timed out for acc=%d target=%s",
                    action,
                    account_id,
                    target,
                )
            except Exception as e:
                etype = type(e).__name__
                error = str(e)[:100]
                success = False

                # ── Ban / dead-session: НЕМЕДЛЕННО остановить и деактивировать ──
                # Продолжать слать запросы с отозванной сессии = эскалация к
                # жёсткому бану и риск для прокси/IP всей когорты.
                if _is_fatal_error(etype, error):
                    log.warning(
                        "warmup: FATAL %s acc=%d — деактивация и остановка сессии",
                        etype,
                        account_id,
                    )
                    try:
                        await pool.execute(
                            "UPDATE tg_accounts SET is_active=FALSE, acc_status='banned' WHERE id=$1",
                            account_id,
                        )
                    except Exception:
                        log_exc_swallow(log, "warmup: deactivate on fatal failed")
                    try:
                        await pool.execute(
                            "UPDATE account_warmup_plans SET status='paused' WHERE id=$1",
                            plan_id,
                        )
                    except Exception:
                        log_exc_swallow(log, "warmup: pause plan on fatal failed")
                    await _log_warmup_action(
                        pool, account_id, action, target, False, error
                    )
                    break

                # ── PEER_FLOOD / spam-restriction: аккаунт жив, но ограничен →
                # СТОП разогрева, пауза плана. Не добивать флагнутый аккаунт. ──
                if _is_restriction_error(etype, error):
                    log.warning(
                        "warmup: RESTRICTION %s acc=%d — пауза плана, стоп сессии",
                        etype,
                        account_id,
                    )
                    try:
                        await pool.execute(
                            "UPDATE account_warmup_plans SET status='paused' WHERE id=$1",
                            plan_id,
                        )
                    except Exception:
                        log_exc_swallow(log, "warmup: pause plan on restriction failed")
                    await _log_warmup_action(
                        pool, account_id, action, target, False, error
                    )
                    break

                # ── FloodWait: спим РОВНО столько, сколько просит Telegram (+jitter).
                # Очень длинный flood (>30 мин) → пауза плана, не блокируем задачу. ──
                if etype == "FloodWaitError":
                    fw_secs = int(getattr(e, "seconds", 60) or 60)
                    if fw_secs > _MAX_FLOOD_WAIT_INLINE:
                        log.warning(
                            "warmup: длинный FloodWait %ds acc=%d — пауза плана, стоп",
                            fw_secs,
                            account_id,
                        )
                        try:
                            await pool.execute(
                                "UPDATE account_warmup_plans SET status='paused' WHERE id=$1",
                                plan_id,
                            )
                        except Exception:
                            log_exc_swallow(log, "warmup: pause on long flood failed")
                        await _log_warmup_action(
                            pool, account_id, action, target, False, error
                        )
                        break
                    log.warning(
                        "warmup: FloodWait %ds on action %s acc=%d — sleeping exact",
                        fw_secs,
                        action,
                        account_id,
                    )
                    await asyncio.sleep(fw_secs + random.uniform(5, 15))

            await _log_warmup_action(pool, account_id, action, target, success, error)
            _action_dur = time.monotonic() - t0_action
            if success:
                infra_memory.record_account_op(
                    account_id, "warmup", True, duration_s=_action_dur
                )
            else:
                infra_memory.record_account_op(
                    account_id,
                    "warmup",
                    False,
                    str(error)[:100] if error else "",
                    duration_s=_action_dur,
                )

            if success:
                actions_ok += 1
                consecutive_fails = 0
            else:
                actions_fail += 1
                consecutive_fails += 1

            # Прогресс-коллбэк после каждого действия
            if update_callback is not None:
                step_desc = _action_descriptions.get(action, action)
                status_icon = "✅" if success else "❌"
                try:
                    update_callback(i + 1, day_actions_n, f"{status_icon} {step_desc}")
                except Exception as cb_exc:
                    log.debug("warmup update_callback error: %s", cb_exc)

            # Адаптивная пауза: учитываем серию ошибок
            if i < day_actions_n - 1:
                if consecutive_fails >= 3:
                    base_pause = random.uniform(120, 300)
                    log.info(
                        "warmup: adaptive pause %.0fs (acc=%d, %d cons.fails)",
                        base_pause,
                        account_id,
                        consecutive_fails,
                    )
                    consecutive_fails = 0
                elif consecutive_fails >= 2:
                    base_pause = random.uniform(45, 120)
                elif (i + 1) % 5 == 0:
                    base_pause = random.uniform(120, 300)
                else:
                    base_pause = random.uniform(20, 90)
                await asyncio.sleep(base_pause)

    except Exception as e:
        etype = type(e).__name__
        if etype in ("AuthKeyUnregisteredError", "SessionRevokedError"):
            log.warning(
                "warmup: fatal auth error acc=%d (%s) — deactivating", account_id, etype
            )
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1",
                    account_id,
                )
            except Exception as db_exc:
                log.error("warmup: failed to deactivate acc=%d: %s", account_id, db_exc)
        else:
            log.warning("warmup session error acc=%d: %s", account_id, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "сбой disconnect при разогреве аккаунта")
        # Освобождаем claim аккаунта, чтобы op_worker мог снова его использовать
        try:
            from services import op_worker as _opw

            await _opw.release_accounts([account_id])
        except Exception:
            log_exc_swallow(log, "warmup: release_accounts failed")

    # Вычисляем уровень прогрева по числу успешных действий
    warmup_level = _compute_warmup_level(actions_ok)

    # Обновляем план — только если было хотя бы частичное выполнение
    # Если все действия провалились, повторяем тот же день на следующем цикле
    if actions_ok > 0:
        new_day = current_day + 1
    else:
        log.warning(
            "warmup: all %d actions failed for acc=%d, retrying same day %d",
            daily_actions,
            account_id,
            current_day,
        )
        new_day = current_day

    completed = new_day >= plan["target_days"]
    new_status = "completed" if completed else "active"

    await pool.execute(
        """UPDATE account_warmup_plans
           SET current_day=$1, status=$2, last_action_at=NOW(),
               completed_at=CASE WHEN $2='completed' THEN NOW() ELSE NULL END
           WHERE id=$3""",
        new_day,
        new_status,
        plan_id,
    )

    if actions_ok > 0:
        # Сохраняем дату последнего прогрева и уровень в аккаунте
        await pool.execute(
            """UPDATE tg_accounts
               SET last_warmup_at = NOW(),
                   warmup_level = $2
               WHERE id = $1""",
            account_id,
            warmup_level,
        )

    if completed and actions_ok > 0:
        # После успешного завершения разогрева повышаем trust_score
        await pool.execute(
            "UPDATE tg_accounts SET trust_score = LEAST(trust_score + 0.3, 1.0) WHERE id=$1",
            account_id,
        )

    readiness_score = None
    readiness_level = warmup_level
    try:
        from services.account_readiness import refresh_account_readiness

        readiness = await refresh_account_readiness(pool, account_id, owner_id)
        if readiness is not None:
            readiness_score = readiness.score
            readiness_level = readiness.level
    except Exception:
        log_exc_swallow(
            log,
            "warmup readiness refresh failed",
            account_id=account_id,
            owner_id=owner_id,
        )

    # Записываем итог дня в operation_audit → виден в "TG-операции" логе
    try:
        from services.op_worker import write_op_audit as _write_op_audit

        total = actions_ok + actions_fail
        if actions_ok == 0:
            _wu_result, _wu_err = "error", f"all {total} actions failed"
        elif actions_fail > 0:
            _wu_result, _wu_err = "partial", f"{actions_fail}/{total} failed"
        else:
            _wu_result, _wu_err = "success", None
        await _write_op_audit(
            pool,
            owner_id=owner_id,
            action="warmup",
            result=_wu_result,
            target=f"day {current_day}",
            account_id=account_id,
            error_msg=_wu_err,
        )
    except Exception:
        pass

    return {
        "actions_done": actions_ok + actions_fail,
        "actions_ok": actions_ok,
        "actions_fail": actions_fail,
        "completed": completed,
        "warmup_level": warmup_level,
        "readiness_score": readiness_score,
        "readiness_level": readiness_level,
    }


async def run_warmup_session(pool: asyncpg.Pool, session: dict) -> dict:
    """
    Выполняет один день прогрева для сессии (N аккаунтов → M целей).

    Возвращает {'actions_done', 'actions_ok', 'actions_fail', 'completed'}.
    """
    session_id = session["id"]

    # Защита от параллельного запуска одной и той же сессии
    session_lock = await _get_session_lock(session_id)
    if session_lock.locked():
        log.info(
            "warmup_session: session %d already running, skipping concurrent launch",
            session_id,
        )
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
        }
    async with session_lock:
        return await _run_warmup_session_impl(pool, session, session_id)


async def _run_warmup_session_impl(
    pool: asyncpg.Pool, session: dict, session_id: int
) -> dict:
    """Внутренняя реализация run_warmup_session — вызывается только через guard."""
    from services import account_manager

    owner_id = session["owner_id"]
    account_ids: list = session.get("account_ids") or []
    target_refs: list = session.get("target_refs") or []
    plan_type: str = session.get("plan_type", "standard")  # noqa: F841 — kept for log context
    current_day: int = session.get("current_day", 0)
    daily_actions: int = session.get("daily_actions", 10)
    target_days: int = session.get("target_days", 14)

    if not account_ids:
        log.warning("warmup_session %d: no account_ids", session_id)
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
        }

    # Если нет явных целей — загружаем из собственной инфраструктуры
    targets: list[str] = list(target_refs) if target_refs else []
    if not targets:
        resources = await _get_warmup_resources(pool, owner_id)
        targets = [
            f"@{c['username']}" for c in resources["channels"] if c.get("username")
        ]
        targets += [f"@{b['username']}" for b in resources["bots"] if b.get("username")]
    if not targets:
        targets = _WARMUP_PUBLIC_CHANNELS[:8]

    # Рампа: на ранних днях каждый аккаунт делает меньше действий
    _target_per_acc = max(1, daily_actions // len(account_ids))
    actions_per_acc = _actions_for_day_count(current_day, _target_per_acc)
    total_ok = 0
    total_fail = 0

    for acc_id in account_ids:
        # Пропускаем аккаунт если он сейчас занят op_worker-операцией,
        # затем СРАЗУ клеймим его, чтобы op_worker не начал операцию во время разогрева.
        _claimed = False
        try:
            from services import op_worker as _opw

            if _opw.is_account_in_use(acc_id):
                log.info(
                    "warmup_session: acc=%d in use by op_worker, skipping this cycle",
                    acc_id,
                )
                continue
            await _opw.mark_accounts_in_use([acc_id])
            _claimed = True
        except Exception as e:
            log.warning(
                "warmup_session: claim check failed acc=%d: %s", acc_id, e
            )
            # Proceed with warmup if check fails - better than skipping

        # Health/ban gate: не разгоняем забаненные/неактивные аккаунты
        _acc_h = await pool.fetchrow(
            "SELECT is_active, acc_status FROM tg_accounts WHERE id=$1", acc_id
        )
        if _acc_h and (
            _acc_h["is_active"] is False
            or (_acc_h["acc_status"] or "active")
            in ("banned", "spamblock", "deactivated")
        ):
            log.info("warmup_session: acc=%d неактивен/забанен — пропуск", acc_id)
            if _claimed:
                try:
                    from services import op_worker as _opw

                    await _opw.release_accounts([acc_id])
                except Exception:
                    log_exc_swallow(log, "warmup_session: release on skip failed")
            continue

        acc_row = await db.get_account_for_telethon(pool, acc_id)
        if not acc_row or not acc_row["session_str"]:
            if _claimed:
                try:
                    from services import op_worker as _opw

                    await _opw.release_accounts([acc_id])
                except Exception:
                    log_exc_swallow(log, "warmup_session: release on no-session failed")
            continue

        device = dict(acc_row) if acc_row["device_model"] else None
        client = account_manager._make_client(acc_row["session_str"], device)
        available_actions = _get_actions_for_day(current_day)

        try:
            await asyncio.wait_for(client.connect(), timeout=15)

            for i in range(actions_per_acc):
                action = random.choice(available_actions)
                target = targets[i % len(targets)] if targets else ""
                success = False
                error_str: Optional[str] = None
                t0 = time.monotonic()

                try:
                    if action in ("update_presence", "browse_dialogs"):
                        target = "self"
                        success = await (
                            _perform_update_presence(client)
                            if action == "update_presence"
                            else _perform_browse_dialogs(client)
                        )
                    elif action == "read_channel" and target:
                        success = await _perform_read_channel(client, target)
                    elif action == "send_reaction" and target:
                        success = await _perform_send_reaction(client, target)
                    elif action == "mark_read" and target:
                        success = await _perform_mark_read(client, target)
                    elif action == "send_comment" and target:
                        success = await _perform_send_comment(client, target)
                    elif action == "forward_to_saved" and target:
                        success = await _perform_forward_to_saved(client, target)
                    elif action == "vote_poll" and target:
                        success = await _perform_vote_poll(client, target)
                    elif action == "own_channel_read" and target:
                        success = await _perform_own_channel_read(client, target)
                    elif action in ("smart_bot_start", "own_bot_start") and target:
                        success = await _perform_smart_bot_cmd(client, target, "/start")
                    elif action == "smart_bot_help" and target:
                        success = await _perform_smart_bot_cmd(client, target, "/help")
                    else:
                        success = True

                    dur_s = time.monotonic() - t0
                    try:
                        from services import infra_memory

                        infra_memory.record_account_op(
                            acc_id, "warmup_session", success, duration_s=dur_s
                        )
                    except Exception as e:
                        log.warning(
                            "warmup_session: infra_memory record failed acc=%d: %s",
                            acc_id,
                            e,
                        )
                except Exception as exc:
                    error_str = str(exc)[:200]
                    success = False
                    _etype = type(exc).__name__

                    # Ban / dead session → деактивировать и прекратить этот аккаунт
                    if _is_fatal_error(_etype, error_str):
                        log.warning(
                            "warmup_session: FATAL %s acc=%d — деактивация, стоп",
                            _etype,
                            acc_id,
                        )
                        try:
                            await pool.execute(
                                "UPDATE tg_accounts SET is_active=FALSE, acc_status='banned' WHERE id=$1",
                                acc_id,
                            )
                        except Exception:
                            log_exc_swallow(log, "warmup_session: deactivate failed")
                        total_fail += 1
                        break
                    # PEER_FLOOD / spam → стоп этого аккаунта (не добивать)
                    if _is_restriction_error(_etype, error_str):
                        log.warning(
                            "warmup_session: RESTRICTION %s acc=%d — стоп аккаунта",
                            _etype,
                            acc_id,
                        )
                        total_fail += 1
                        break
                    # FloodWait → спим ровно столько, сколько просит Telegram
                    if _etype == "FloodWaitError":
                        _fw = int(getattr(exc, "seconds", 60) or 60)
                        if _fw > _MAX_FLOOD_WAIT_INLINE:
                            log.warning(
                                "warmup_session: длинный FloodWait %ds acc=%d — стоп",
                                _fw,
                                acc_id,
                            )
                            total_fail += 1
                            break
                        log.warning(
                            "warmup_session: FloodWait %ds acc=%d — sleeping exact",
                            _fw,
                            acc_id,
                        )
                        await asyncio.sleep(_fw + random.uniform(5, 15))

                if success:
                    total_ok += 1
                else:
                    total_fail += 1

                try:
                    await pool.execute(
                        """INSERT INTO warmup_session_log
                           (session_id, account_id, action_type, target, success, error)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        session_id,
                        acc_id,
                        action,
                        target or "",
                        success,
                        error_str,
                    )
                except Exception as e:
                    log.warning(
                        "warmup_session: DB log insert failed acc=%d: %s", acc_id, e
                    )

                await asyncio.sleep(random.uniform(8, 25))

        except Exception as exc:
            log.warning("warmup_session acc=%d: %s", acc_id, exc)
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception:
                pass
            # Освобождаем claim аккаунта
            if _claimed:
                try:
                    from services import op_worker as _opw

                    await _opw.release_accounts([acc_id])
                except Exception:
                    log_exc_swallow(log, "warmup_session: release_accounts failed")

        await asyncio.sleep(random.uniform(15, 45))

    # Advance day only if at least one action succeeded — mirror run_daily_warmup logic
    if total_ok > 0:
        new_day = current_day + 1
    else:
        log.warning(
            "warmup_session %d: all actions failed, retrying same day %d on next cycle",
            session_id,
            current_day,
        )
        new_day = current_day
    completed = new_day >= target_days
    new_status = "completed" if completed else "active"

    await pool.execute(
        """UPDATE warmup_sessions
           SET current_day=$1, last_run_at=NOW(), status=$2
           WHERE id=$3""",
        new_day,
        new_status,
        session_id,
    )

    log.info(
        "warmup_session %d day=%d/%d ok=%d fail=%d completed=%s",
        session_id,
        new_day,
        target_days,
        total_ok,
        total_fail,
        completed,
    )
    return {
        "actions_done": total_ok + total_fail,
        "actions_ok": total_ok,
        "actions_fail": total_fail,
        "completed": completed,
    }


_MAX_PARALLEL_WARMUP = 2  # максимум одновременных warmup-планов/сессий; >2 triggers Telegram coordinated-activity detection


async def run_warmup_loop(pool: asyncpg.Pool, interval_hours: int = 1) -> None:
    """
    Фоновый цикл: каждый час проверяет активные планы И сессии разогрева.
    Один запуск в сутки на план/сессию (проверяем last_action_at > 20ч).
    Планы и сессии запускаются ПАРАЛЛЕЛЬНО (до _MAX_PARALLEL_WARMUP одновременно).
    """
    while True:
        try:
            # Одиночные планы разогрева
            rows = await pool.fetch(
                """SELECT wp.*, a.owner_id
                   FROM account_warmup_plans wp
                   JOIN tg_accounts a ON a.id = wp.account_id
                   WHERE wp.status = 'active'
                     AND (wp.last_action_at IS NULL
                          OR wp.last_action_at < NOW() - INTERVAL '20 hours')""",
            )
            if rows:
                log.info("warmup loop: %d single-plans to run (parallel)", len(rows))
            # Запускаем батчами по _MAX_PARALLEL_WARMUP, не блокируем loop
            for i in range(0, len(rows), _MAX_PARALLEL_WARMUP):
                batch = rows[i : i + _MAX_PARALLEL_WARMUP]
                tasks = [
                    asyncio.create_task(run_daily_warmup(pool, dict(p))) for p in batch
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

            # Мультиаккаунтные сессии прогрева
            session_rows = await pool.fetch(
                """SELECT * FROM warmup_sessions
                   WHERE status = 'active'
                     AND (last_run_at IS NULL
                          OR last_run_at < NOW() - INTERVAL '20 hours')""",
            )
            if session_rows:
                log.info(
                    "warmup loop: %d sessions to run (parallel)", len(session_rows)
                )
            for i in range(0, len(session_rows), _MAX_PARALLEL_WARMUP):
                batch = session_rows[i : i + _MAX_PARALLEL_WARMUP]
                tasks = [
                    asyncio.create_task(run_warmup_session(pool, dict(s)))
                    for s in batch
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            log.warning("warmup loop error: %s", e)
        await asyncio.sleep(interval_hours * 3600)
