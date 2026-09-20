"""Account Warming System — gradual warming of new accounts.

Simulates natural behavior:
  - Day 1-3: reading messages, viewing profiles
  - Day 4-7: likes/reactions, joining channels
  - Day 8-14: comments, group messages
  - Day 15+: full activity

All actions are logged to account_warmup_log.
Plan status is stored in account_warmup_plans.

Usage:
    from services.account_warmer import run_warmup_loop

    # Starts the background warming loop
    await run_warmup_loop(pool)
"""

from __future__ import annotations

import asyncio
import datetime
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


def _account_channels(account_id: int | None, source: list[str], k: int = 8) -> list[str]:
    """Стабильный ПЕР-АККАУНТНЫЙ набор и порядок каналов из общего пула.

    Раньше сессионный путь брал `_WARMUP_PUBLIC_CHANNELS[:8]` — одни и те же 8
    каналов для КАЖДОГО аккаунта всего флота, а путь планов делал глобальный
    random.shuffle (набор тот же, порядок скачет каждый день). Совпадающий граф
    вступлений кластеризует когорту — прогрев сам создавал сигнатуру ботнета.

    Сид от account_id даёт: (а) у каждого аккаунта свой набор/порядок,
    (б) он СТАБИЛЕН во времени — живой человек не перевыбирает интересы каждый
    день. account_id=None → прежнее поведение (первые k), чтобы не ломать
    вызовы без контекста аккаунта.
    """
    if not source:
        return []
    k = max(1, min(int(k), len(source)))
    if account_id is None:
        return list(source[:k])
    rnd = random.Random(f"warmup-channels:{int(account_id)}")
    picked = list(source)
    rnd.shuffle(picked)
    return picked[:k]


# Spintax-шаблоны комментариев прогрева: текст генерируется движком проекта
# (services/spintax_engine) с сидом от аккаунта+поста. Раньше был
# random.choice из 19 фиксированных фраз — весь флот писал одинаковые тексты,
# что тривиально детектируется.
_COMMENT_SPINTAX = [
    # Каждая ветка самодостаточна: любая комбинация читается грамотно —
    # неестественный текст сам по себе является детект-сигналом.
    "{Согласен|Соглашусь|Полностью согласен}{.|!}",
    "{Спасибо!|Спасибо за пост!|Благодарю!|Благодарю за пост!}",
    "{Интересно|Любопытно|Занятно}{.|!}",
    "{Не знал|Не думал об этом|Надо обдумать}{.|!}",
    "{Хороший|Годный|Толковый} {пост|материал|разбор}{.|!}",
    "{Полезно|Пригодится|Забрал в закладки}{.|!}",
    "{Актуально|В точку|Верно подмечено}{.|!}",
    "{Согласен|Соглашусь}, {дельно|по делу|толково}{.|!}",
    "{Спасибо|Благодарю}, {полезно|пригодится|интересно}{.|!}",
]


def _warm_comment_text(account_id: int | None = None, salt: str | int = "") -> str:
    """Сгенерировать текст комментария прогрева (spintax, сид от аккаунта).

    Fail-soft: при любой ошибке движка откатываемся на исторический список —
    прогрев не должен падать из-за генерации текста.
    """
    try:
        from services.spintax_engine import SpintaxEngine

        tpl_rnd = random.Random(f"warmup-tpl:{account_id}:{salt}")
        template = tpl_rnd.choice(_COMMENT_SPINTAX)
        text = SpintaxEngine().generate(
            template, seed=f"warmup-comment:{account_id}:{salt}"
        )
        text = (text or "").strip()
        if text:
            return text
    except Exception:
        log_exc_swallow(log, "warmup: spintax-комментарий не сгенерирован")
    return random.choice(_COMMENT_TEXTS)


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


WARMUP_PROFILES = ("reader", "commenter", "reactor", "lurker", "mixed")
WARMUP_NICHES = ("general", "tech", "news", "crypto", "sports", "entertainment")


def normalize_warmup_channels(raw, limit: int = 50) -> list[str]:
    """Приводит пользовательский ввод каналов к списку валидных @username.

    Принимает список или строку (каналы через перенос/запятую). Отсекает мусор,
    поддерживает t.me/<name> и @name, дедуплицирует с сохранением порядка,
    ограничивает количество (защита от гигантских списков). Общий хелпер для
    UI-эндпоинта прогрева и любых других мест, где нужен разбор каналов.
    """
    import re as _re

    if isinstance(raw, str):
        raw = _re.split(r"[\n,]+", raw)
    out: list[str] = []
    for ch in (raw or []):
        ch = str(ch).strip().lstrip("@").strip()
        if not ch:
            continue
        m = _re.match(r"^(?:https?://t\.me/|t\.me/)?([A-Za-z0-9_]{3,32})/?$", ch)
        if m:
            out.append("@" + m.group(1))
    return list(dict.fromkeys(out))[:limit]


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

    Также выставляет acc_status='warming' чтобы ресурс-селектор и UI
    могли отличить прогреваемые аккаунты от обычных активных.
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
    plan_id = row["id"]

    # Mark the account as warming so resource_selector excludes it from
    # heavy ops and UI shows the correct warming badge.
    try:
        await pool.execute(
            """UPDATE tg_accounts
               SET acc_status = 'warming'
               WHERE id = $1
                 AND is_active = TRUE
                 AND COALESCE(acc_status, 'active') = 'active'""",
            account_id,
        )
    except Exception as _e:
        log.warning("warmup: could not set acc_status=warming for acc=%d: %s", account_id, _e)

    # CRM-воронка: старт прогрева → авто-стадия «Прогрев» (только «сырой» аккаунт).
    try:
        await db.apply_account_stage_event(pool, account_id, "warmup_start")
    except Exception:
        log_exc_swallow(log, "warmup: stage(warmup_start) failed")

    # Update in-memory health cache warmup_state immediately
    try:
        from services import account_health as _ah
        health = _ah.get_health(account_id)
        health.warmup_state = _ah.WarmupState.WARMING
    except Exception:
        log_exc_swallow(log, "warmup: set warmup_state=warming failed")

    log.info("warmup: created plan %d for acc=%d (acc_status → warming)", plan_id, account_id)
    return plan_id


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


_WARM_SAFE_ACTIONS = ("update_presence", "browse_dialogs", "read_channel")
_WARM_RISKY_L1 = ("send_comment", "send_reaction", "forward_to_saved")


def _maturity_level(age_days: float | None, warmup_day: int | None) -> int:
    """Зрелость аккаунта 0..3 по возрасту и пройденным дням прогрева.

    Неизвестное значение НЕ ограничивает (999) — ограничивает второй сигнал.
    """
    a = 999.0 if age_days is None else float(age_days)
    d = 999 if warmup_day is None else int(warmup_day)
    if a < 2 or d < 3:
        return 0
    if a < 7 or d < 7:
        return 1
    if a < 14 or d < 14:
        return 2
    return 3


def _health_level(trust_score: float | None) -> int:
    """Здоровье 0..3 по trust_score (шкала 0..1, пороги как во всём проекте)."""
    t = 1.0 if trust_score is None else float(trust_score)
    if t < 0.3:
        return 0
    if t < 0.5:
        return 1
    if t < 0.7:
        return 2
    return 3


def _progressive_actions(
    available: list[str],
    *,
    trust_score: float | None = None,
    age_days: float | None = None,
    warmup_day: int | None = None,
) -> list[str]:
    """Урезать набор действий прогрева по СТРОГОМУ из двух сигналов.

    Раньше гейт зависел только от trust_score, но trust_score в проекте — это
    ЗДОРОВЬЕ: здоровый аккаунт получает 1.0 с первого дня, поэтому гейт
    фактически не ограничивал ничего — свежий аккаунт с нуля писал комментарии
    и ставил реакции (сильный ban-сигнал). В пути одиночных планов гейта не
    было вовсе. Теперь ограничивает и зрелость (возраст + день прогрева).

    Пустой список никогда не возвращается: цепочка фильтров могла обнулить
    набор, и random.choice() упал бы с IndexError.
    """
    level = min(_maturity_level(age_days, warmup_day), _health_level(trust_score))
    if level <= 0:
        out = [a for a in available if a in _WARM_SAFE_ACTIONS]
    elif level == 1:
        out = [a for a in available if a not in _WARM_RISKY_L1]
    elif level == 2:
        out = [a for a in available if a != "send_comment"]
    else:
        out = list(available)
    if not out:
        out = [a for a in available if a in _WARM_SAFE_ACTIONS] or list(_WARM_SAFE_ACTIONS)
    return out


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

# Серия подряд провалившихся действий (не классифицированных как fatal/restriction/
# flood — таймауты, generic-ошибки, недоступные каналы). Высокая серия сама по себе
# сигнал «что-то не так с аккаунтом» — продолжать долбить = риск. При достижении —
# прерываем ДНЕВНОЙ прогон (план не паузим: следующий цикл повторит, вдруг транзиент).
_WARMUP_MAX_FAIL_STREAK = 6


def _fail_streak_abort(fail_streak: int) -> bool:
    """True если серия провалов достигла потолка безопасности — прервать дневной прогон."""
    return fail_streak >= _WARMUP_MAX_FAIL_STREAK


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

def _time_of_day_multiplier(geo_country: str | None = None, account_id=None) -> float:
    """Множитель темпа по ЛОКАЛЬНОМУ времени аккаунта (гео его прокси).

    Раньше весь флот жил по захардкоженному Киеву (UTC+2, без учёта DST):
    аккаунт на US-прокси «бодрствовал» в киевские часы — поведенческий
    рассинхрон с заявленным гео, который Telegram видит. `geo_tempo` уже
    использовался op_worker/ghost_engine/chat_warmup; прогрев оставался
    единственной подсистемой на хардкоде.

    При неизвестном гео geo_tempo сам откатывается на серверное время — без
    регрессии. `account_id` накладывает персональный хронотип: флот не
    замедляется синхронно в один и тот же час (анти-сигнатура ботнета).
    """
    from services import geo_tempo

    return geo_tempo.local_factor(geo_country, account_id=account_id)


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


async def _perform_send_comment(client, channel_ref: str, account_id: int | None = None) -> bool:
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
        comment = _warm_comment_text(account_id, salt=getattr(post, 'id', ''))
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
                log_exc_swallow(log, "warmup: send reaction failed")
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


async def _note_skip(pool: asyncpg.Pool, plan_id: int, reason: str) -> None:
    """Запомнить, ПОЧЕМУ день прогрева пропущен.

    Раньше все шесть путей пропуска писали только в серверный лог, а план
    оставался «🟢 Активен · День 3/21». Снаружи мёртвый аккаунт был неотличим
    от здорового, и человек узнавал правду через недели простоя.
    """
    try:
        await pool.execute(
            """UPDATE account_warmup_plans
                  SET last_skip_reason=$2, last_skip_at=NOW()
                WHERE id=$1""",
            plan_id, reason)
    except Exception:
        log.debug("warmup: не удалось записать причину пропуска plan=%s", plan_id)


async def _pause_plan(pool: asyncpg.Pool, plan_id: int, reason: str,
                      detail: str | None = None) -> None:
    """Остановить план С УКАЗАНИЕМ причины.

    Пауза движка (бан, спам-блок, длинный FloodWait) и пауза человека — разные
    вещи: первую нельзя молча предлагать «возобновить», иначе пользователь
    своими руками добивает флагнутый аккаунт. Причина решает это на уровне
    данных, а не подсказок в интерфейсе.
    """
    try:
        await pool.execute(
            """UPDATE account_warmup_plans
                  SET status='paused', pause_reason=$2, pause_detail=$3,
                      paused_at=NOW(), pause_notified_at=NULL
                WHERE id=$1""",
            plan_id, reason, (str(detail)[:300] if detail else None))
    except Exception as e:
        log.warning("warmup: пауза плана %s (%s) не записана: %s", plan_id, reason, e)


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
        await _note_skip(pool, plan_id, "not_found")
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
            "warmup_level": "light",
        }

    if not acc_row["session_str"]:
        log.warning("warmup: account %d has no session_str, skipping", account_id)
        await _note_skip(pool, plan_id, "no_session")
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
            await _note_skip(pool, plan_id, "busy")
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
            await _note_skip(pool, plan_id, "inactive")
            return _skip_result
        if (acc_health["acc_status"] or "active") in (
            "banned",
            "spamblock",
            "deactivated",
            # session_expired — сессия мертва (напр. AuthKeyUnregistered при синке
            # контактов): разогрев бессмыслен, коннект всё равно упадёт. Иммунный
            # сигнал → метаболизм (Волна I): не жжём циклы на дохлую сессию.
            "session_expired",
        ):
            log.info(
                "warmup: acc=%d статус=%s — пропуск разогрева",
                account_id,
                acc_health["acc_status"],
            )
            await _note_skip(pool, plan_id, str(acc_health["acc_status"]))
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
    # Атомарный захват (check-and-set под одним локом) вместо проверки-затем-пометки:
    # закрывает TOCTOU-окно, где op_worker захватывал аккаунт между is_account_in_use
    # (выше) и mark. Если аккаунт уже занят операцией — прогрев пропускаем, иначе
    # одна сессия коннектится прогревом И операцией → AUTH_KEY_DUPLICATED.
    try:
        from services import op_worker as _opw

        _leased = await _opw.try_claim_account(account_id)
    except Exception:
        log_exc_swallow(log, "warmup: try_claim_account failed")
        _leased = True  # op_worker недоступен — best-effort как раньше
    if not _leased:
        log.info("warmup: acc=%d занят операцией/циклом — пропуск прогрева", account_id)
        await _note_skip(pool, plan_id, "busy")
        return _skip_result

    # Счётчики — ДО try: пост-try код (warmup_level/обновление плана) читает их,
    # а исключение в окне подготовки могло случиться раньше их инициализации
    # внутри try → UnboundLocalError вместо честного обновления плана.
    actions_ok = 0
    actions_fail = 0
    # ВЕСЬ пост-захватный путь под общим try/finally (release в finally ниже):
    # раньше окно от захвата до client.connect() (niche/health/_make_client) НЕ
    # освобождало аккаунт при исключении — он «зомби» в памяти до рестарта, и
    # ни renew_leases, ни reconcile его не чистят (память считает занятым).
    try:
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
        fail_streak = 0  # подряд провалов (сброс только на успехе) — потолок безопасности
        available_actions = _get_actions_for_day(current_day)
        # Прогрессивный гейт: в этом пути (одиночные планы) его не было вовсе —
        # свежий аккаунт мог с первого дня писать комментарии и ставить реакции.
        _p_age_days = None
        _p_trust = None
        if acc_health is not None:
            try:
                from datetime import datetime as _dtm2, timezone as _tzz2

                _p_trust = acc_health["trust_score"]
                if acc_health["added_at"] is not None:
                    _p_age_days = (
                        _dtm2.now(_tzz2.utc) - acc_health["added_at"].replace(tzinfo=_tzz2.utc)
                    ).total_seconds() / 86400.0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
        available_actions = _progressive_actions(
            available_actions,
            trust_score=_p_trust,
            age_days=_p_age_days,
            warmup_day=current_day,
        )
        resources = await _get_warmup_resources(pool, owner_id)
        own_bots = resources["bots"]
        own_channels = resources["channels"]
        # Нишево-осведомлённый список каналов: через account_niche_profiles
        channels = await get_account_niche_channels(pool, account_id)
        if not channels:
            channels = list(_WARMUP_PUBLIC_CHANNELS)
        # Стабильный пер-аккаунтный порядок/набор вместо глобального shuffle:
        # одинаковый граф вступлений у всего флота — сигнатура ботнета.
        channels = _account_channels(account_id, channels, k=len(channels))

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
        # Темп — по локальному времени гео аккаунта + личный хронотип (не по Киеву).
        _tod_mult = _time_of_day_multiplier(plan.get("geo_country"), account_id=account_id)
        if _tod_mult != 1.0:
            day_actions_n = max(1, int(day_actions_n * _tod_mult))

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
                    await asyncio.sleep(random.uniform(2.0, 8.0))
                    await asyncio.sleep(random.uniform(1.0, 4.0))
                    success = await asyncio.wait_for(
                        _perform_send_comment(client, target, account_id), timeout=90
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
                    await asyncio.sleep(random.uniform(2.0, 8.0))
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
                    for _retry in range(3):
                        try:
                            await pool.execute(
                                "UPDATE tg_accounts SET is_active=FALSE, acc_status='banned' WHERE id=$1",
                                account_id,
                            )
                            break
                        except Exception as e:
                            if _retry == 2:
                                log.warning("warmup: CRITICAL failed to deactivate banned acc=%d after 3 retries: %s", account_id, e)
                            else:
                                await asyncio.sleep(1)
                    # CRM-воронка: мёртвый аккаунт → авто-стадия «Заморожен».
                    try:
                        await db.apply_account_stage_event(pool, account_id, "banned")
                    except Exception:
                        log_exc_swallow(log, "warmup: stage(banned) failed")
                    await _pause_plan(pool, plan_id, "banned", error or etype)
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
                    await _pause_plan(pool, plan_id, "restricted", error or etype)
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
                        await _pause_plan(
                            pool, plan_id, "flood",
                            f"Telegram просит подождать {fw_secs} с")
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
                # Update in-memory health score on each successful warmup action
                try:
                    from services import account_health as _ah
                    _ah.update_after_success(account_id, action)
                except Exception:
                    log_exc_swallow(log, "warmup: update health after success failed")
            else:
                infra_memory.record_account_op(
                    account_id,
                    "warmup",
                    False,
                    str(error)[:100] if error else "",
                    duration_s=_action_dur,
                )
                # Update in-memory health score on each failed warmup action
                try:
                    from services import account_health as _ah
                    _is_flood = error is not None and "flood" in str(error).lower()
                    _ah.update_after_failure(account_id, action, is_flood=_is_flood)
                except Exception:
                    log_exc_swallow(log, "warmup: update health after failure failed")

            if success:
                actions_ok += 1
                consecutive_fails = 0
                fail_streak = 0
            else:
                actions_fail += 1
                consecutive_fails += 1
                fail_streak += 1

            # Потолок безопасности: длинная серия провалов (даже не-классифицированных)
            # — сигнал, что аккаунт нездоров. Не добиваем его до конца дневного бюджета.
            if _fail_streak_abort(fail_streak):
                log.warning(
                    "warmup: acc=%d — %d провалов подряд, прерываю дневной прогон",
                    account_id, fail_streak,
                )
                break

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
                    base_pause = random.uniform(60, 120)
                elif (i + 1) % 5 == 0 and random.random() < 0.15:
                    base_pause = random.uniform(300, 1800)
                elif (i + 1) % 5 == 0:
                    base_pause = random.uniform(120, 300)
                else:
                    # Minimum 30s between actions to avoid Telegram automation detection
                    base_pause = random.uniform(30, 90)
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

    # Причину пропуска ведём вместе с днём: успешный день её снимает, полностью
    # проваленный — записывает. Иначе экран показывал бы старую жалобу на плане,
    # который давно ожил, либо молчал бы о плане, который каждый день пустой.
    await pool.execute(
        # $4::text ОБЯЗАТЕЛЕН: без явного типа Postgres не может вывести тип $4
        # (он используется только в присваивании и в `IS NULL`) и падает на PREPARE
        # c AmbiguousParameterError — КАЖДЫЙ раз, при любом значении. Из-за этого
        # план прогрева НИКОГДА не обновлялся: current_day и last_action_at стояли,
        # исключение глушил gather(return_exceptions=True) в цикле — снаружи это
        # «день 0/14, хотя действия успешные». Каст чинит обновление плана.
        """UPDATE account_warmup_plans
           SET current_day=$1, status=$2, last_action_at=NOW(),
               last_skip_reason=$4::text,
               last_skip_at=CASE WHEN $4::text IS NULL THEN NULL ELSE NOW() END,
               completed_at=CASE WHEN $2='completed' THEN NOW() ELSE NULL END
           WHERE id=$3""",
        new_day,
        new_status,
        plan_id,
        None if actions_ok > 0 else "all_failed",
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
        # После успешного завершения разогрева:
        # 1. Повышаем trust_score
        # 2. Выпускаем аккаунт из состояния warming → active (graduation)
        await pool.execute(
            """UPDATE tg_accounts
               SET trust_score = LEAST(COALESCE(trust_score, 0.5) + 0.3, 1.0),
                   acc_status = CASE
                       WHEN COALESCE(acc_status, 'active') = 'warming' THEN 'active'
                       ELSE COALESCE(acc_status, 'active')
                   END
               WHERE id = $1""",
            account_id,
        )
        # CRM-воронка: прогрев завершён → авто-стадия «Готов» (не перетирая ручные).
        try:
            await db.apply_account_stage_event(pool, account_id, "warmup_done")
        except Exception:
            log_exc_swallow(log, "warmup: apply_account_stage_event(warmup_done) failed")
        # Update in-memory health cache: warmup_state → READY (graduated)
        try:
            from services import account_health as _ah
            health = _ah.get_health(account_id)
            health.warmup_state = _ah.WarmupState.READY
            # Give a final health boost for completing the full warmup plan
            health.health_score = min(100.0, health.health_score + 5.0)
        except Exception:
            log_exc_swallow(log, "warmup: set warmup_state=READY failed")
        log.info(
            "warmup: acc=%d GRADUATED — plan %d completed, acc_status=active, warmup_state=READY",
            account_id,
            plan_id,
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
        log_exc_swallow(log, "warmup: write op audit failed")

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
        targets = list(_WARMUP_PUBLIC_CHANNELS)

    # Рампа: на ранних днях каждый аккаунт делает меньше действий
    _target_per_acc = max(1, daily_actions // len(account_ids))
    # База без времени суток: множитель применяется ПО-АККАУНТНО ниже, по
    # локальному времени гео каждого аккаунта (сессия может держать аккаунты
    # из разных стран — один общий «киевский» множитель здесь был неверен).
    actions_per_acc_base = _actions_for_day_count(current_day, _target_per_acc)
    total_ok = 0
    total_fail = 0

    for acc_id in account_ids:
        # Пропускаем аккаунт если он сейчас занят op_worker-операцией,
        # затем СРАЗУ клеймим его, чтобы op_worker не начал операцию во время разогрева.
        _claimed = False
        try:
            from services import op_worker as _opw

            # Атомарный захват (check-and-set) — устраняет TOCTOU между проверкой
            # занятости и пометкой: op_worker больше не может вклиниться и открыть
            # операцию на той же сессии во время прогрева (AUTH_KEY_DUPLICATED).
            _claimed = await _opw.try_claim_account(acc_id)
            if not _claimed:
                log.info(
                    "warmup_session: acc=%d in use by op_worker, skipping this cycle",
                    acc_id,
                )
                continue
        except Exception as e:
            log.warning(
                "warmup_session: claim check failed acc=%d: %s", acc_id, e
            )
            # Proceed with warmup if check fails - better than skipping

        # Health/ban gate: не разгоняем забаненные/неактивные аккаунты.
        # geo_country берём этим же запросом (без лишнего round-trip) — нужен
        # для локального времени/ночи аккаунта.
        _acc_h = await pool.fetchrow(
            """SELECT a.is_active, a.acc_status, a.added_at, a.trust_score,
                      up.geo_country
               FROM tg_accounts a
               LEFT JOIN user_proxies up ON up.id = a.proxy_id
               WHERE a.id=$1""",
            acc_id,
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

        # Локальная ночь аккаунта (по гео его прокси) — не греем. Сессия может
        # держать аккаунты из разных стран, поэтому решение по-аккаунтное.
        _acc_geo = _acc_h["geo_country"] if _acc_h else None
        from services import geo_tempo as _gt

        if _gt.is_local_night(_acc_geo):
            log.info(
                "warmup_session: acc=%d — локальная ночь (гео=%s), пропуск цикла",
                acc_id, _acc_geo,
            )
            if _claimed:
                try:
                    from services import op_worker as _opw

                    await _opw.release_accounts([acc_id])
                except Exception:
                    log_exc_swallow(log, "warmup_session: release on night skip failed")
            continue

        # Объём действий — по локальному времени ЭТОГО аккаунта + личный хронотип.
        # Пер-аккаунтный набор целей: раньше все аккаунты сессии работали по
        # одному и тому же списку (а фолбэк был вообще _WARMUP_PUBLIC_CHANNELS[:8]
        # для всего флота) — совпадающий граф вступлений кластеризует когорту.
        _acc_targets = _account_channels(acc_id, targets, k=min(8, len(targets))) if targets else []
        _acc_actions = max(
            1,
            int(actions_per_acc_base * _time_of_day_multiplier(_acc_geo, account_id=acc_id)),
        )

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

        # Прогрессивный гейт: по ЗРЕЛОСТИ (возраст + день прогрева) И здоровью.
        # Только trust_score было недостаточно — он равен 1.0 у любого здорового
        # аккаунта с первого дня, т.е. ничего не ограничивал.
        _age_days = None
        _trust = None
        try:
            from datetime import datetime as _dtm, timezone as _tzz

            if _acc_h is not None:
                _trust = _acc_h["trust_score"]
                if _acc_h["added_at"] is not None:
                    _age_days = (
                        _dtm.now(_tzz.utc) - _acc_h["added_at"].replace(tzinfo=_tzz.utc)
                    ).total_seconds() / 86400.0
        except (TypeError, ValueError, AttributeError, KeyError):
            pass
        available_actions = _progressive_actions(
            available_actions,
            trust_score=_trust,
            age_days=_age_days,
            warmup_day=current_day,
        )

        try:
            await asyncio.wait_for(client.connect(), timeout=15)

            for i in range(_acc_actions):
                action = random.choice(available_actions)
                target = _acc_targets[i % len(_acc_targets)] if _acc_targets else ""
                success = False
                error_str: Optional[str] = None
                t0 = time.monotonic()

                # Human-like delays: имитация чтения, typing, пауз
                _human_delay = random.uniform(1.0, 4.0)
                if action in ("read_channel", "browse_dialogs"):
                    _human_delay = random.uniform(3.0, 12.0)  # чтение — дольше
                elif action in ("send_comment", "send_reaction"):
                    _human_delay = random.uniform(0.5, 2.0)  # реакция — быстрее
                await asyncio.sleep(_human_delay)

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
                        await asyncio.sleep(random.uniform(2.0, 8.0))
                        success = await _perform_send_reaction(client, target)
                    elif action == "mark_read" and target:
                        success = await _perform_mark_read(client, target)
                    elif action == "send_comment" and target:
                        await asyncio.sleep(random.uniform(2.0, 8.0))
                        await asyncio.sleep(random.uniform(1.0, 4.0))
                        success = await _perform_send_comment(client, target, acc_id)
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
                        # CRM-воронка: мёртвый аккаунт → авто-стадия «Заморожен».
                        try:
                            await db.apply_account_stage_event(pool, acc_id, "banned")
                        except Exception:
                            log_exc_swallow(log, "warmup_session: stage(banned) failed")
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
                    # Update in-memory health score on successful warmup session action
                    try:
                        from services import account_health as _ah
                        _ah.update_after_success(acc_id, action)
                    except Exception:
                        log_exc_swallow(log, "warmup_session: update health after success failed")
                else:
                    total_fail += 1
                    # Update in-memory health score on failed warmup session action
                    try:
                        from services import account_health as _ah
                        _is_flood = error_str is not None and "flood" in str(error_str).lower()
                        _ah.update_after_failure(acc_id, action, is_flood=_is_flood)
                    except Exception:
                        log_exc_swallow(log, "warmup_session: update health after failure failed")

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

                # Human-like delay between actions: 30-120s (was 8-25s).
                # Sub-30s inter-action intervals are a Telegram automation signal.
                if (i + 1) % 5 == 0 and random.random() < 0.15:
                    await asyncio.sleep(random.uniform(300, 1800))
                else:
                    await asyncio.sleep(random.uniform(30, 120))

        except Exception as exc:
            log.warning("warmup_session acc=%d: %s", acc_id, exc)
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except Exception:
                log_exc_swallow(log, "warmup_session: client disconnect failed")
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

    # Graduate accounts that completed the session: warming → active
    if completed and total_ok > 0:
        session_acc_ids = list(account_ids) if account_ids else []
        for _acc_id in session_acc_ids:
            try:
                await pool.execute(
                    """UPDATE tg_accounts
                       SET trust_score = LEAST(COALESCE(trust_score, 0.5) + 0.2, 1.0),
                           acc_status = CASE
                               WHEN COALESCE(acc_status, 'active') = 'warming' THEN 'active'
                               ELSE COALESCE(acc_status, 'active')
                           END
                       WHERE id = $1""",
                    _acc_id,
                )
                # Update in-memory health cache: warmup_state → READY (graduated)
                try:
                    from services import account_health as _ah
                    health = _ah.get_health(_acc_id)
                    health.warmup_state = _ah.WarmupState.READY
                    health.health_score = min(100.0, health.health_score + 3.0)
                except Exception:
                    log_exc_swallow(log, "warmup_session: set warmup_state=READY failed")
            except Exception as _ge:
                log.warning(
                    "warmup_session: graduation update failed acc=%d: %s", _acc_id, _ge
                )
        log.info(
            "warmup_session %d: GRADUATED %d accounts → acc_status=active, warmup_state=READY",
            session_id,
            len(session_acc_ids),
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


def _proxy_safe_batches(rows: list, batch_size: int) -> list[list]:
    """
    Строит батчи так, чтобы в одном батче не было двух аккаунтов на одном proxy_id.
    Аккаунты без прокси (proxy_id IS NULL) разрешены параллельно между собой.
    """
    result: list[list] = []
    remaining = list(rows)
    while remaining:
        batch: list = []
        used_proxies: set = set()
        leftover: list = []
        for idx, row in enumerate(remaining):
            pid = None
            try:
                pid = row["proxy_id"]
            except (KeyError, TypeError, IndexError):
                pass
            if pid is not None and pid in used_proxies:
                leftover.append(row)
                continue
            batch.append(row)
            if pid is not None:
                used_proxies.add(pid)
            if len(batch) >= batch_size:
                leftover.extend(remaining[idx + 1:])
                break
        result.append(batch)
        remaining = leftover
    return result


async def notify_paused_plans(pool: asyncpg.Pool, bot) -> int:
    """Сообщить владельцу о планах, которые движок остановил САМ.

    До этого о самостоятельной остановке не сообщалось вообще: аккаунт ловил
    бан на четвёртый день из двадцати одного, план вставал, и человек узнавал
    об этом, только если случайно открывал экран прогрева — и то видел лишь
    «⏸ Пауза», неотличимую от своей собственной.

    Сообщаем ОДИН раз на остановку (метка `pause_notified_at`), иначе
    уведомление повторялось бы каждый час и его перестали бы читать.
    Возвращает число отправленных — для тестов и логов.
    """
    from services import warmup_status

    try:
        rows = await pool.fetch(
            """SELECT wp.id, wp.owner_id, wp.pause_reason, wp.pause_detail,
                      wp.current_day, wp.target_days,
                      a.phone, a.first_name
                 FROM account_warmup_plans wp
                 JOIN tg_accounts a ON a.id = wp.account_id
                WHERE wp.status='paused'
                  AND wp.pause_notified_at IS NULL
                  AND wp.pause_reason = ANY($1::text[])
                LIMIT 50""",
            list(warmup_status.AUTO_PAUSE_REASONS))
    except Exception as e:
        log.debug("warmup: выборка остановленных планов не удалась: %s", e)
        return 0

    sent = 0
    for r in rows:
        name = r["first_name"] or r["phone"] or f"аккаунт #{r['id']}"
        text = warmup_status.build_pause_alert(
            name, r["pause_reason"], r["current_day"], r["target_days"],
            r["pause_detail"])
        try:
            from database import db as _db

            await _db.notify_if_enabled(
                pool, bot, r["owner_id"], "restriction", text,
                dedup_key=f"warmup_pause:{r['id']}")
            sent += 1
        except Exception:
            log.debug("warmup: уведомление о паузе не отправлено plan=%s", r["id"])
        # Метку ставим в любом случае: если уведомление не ушло, повторять его
        # каждый час бессмысленно — состояние всё равно видно на экране.
        try:
            await pool.execute(
                "UPDATE account_warmup_plans SET pause_notified_at=NOW() WHERE id=$1",
                r["id"])
        except Exception:
            log.debug("warmup: метка уведомления не записана plan=%s", r["id"])
    return sent


async def _select_due_plans(pool: asyncpg.Pool) -> list:
    """Активные планы прогрева, которым пора запуститься.

    Занятость аккаунта считаем ПО АРЕНДЕ, а не по голому in_operation: флаг мог
    ПРОТЕЧЬ (операция упала, аренда истекла) — тогда аккаунт числился занятым
    ВЕЧНО, и прогрев молча стоял (день 0/14, «занят операцией», N часов без
    действий). Атомарный захват try_claim_account/_db_claim и так считает
    истёкшую/пустую аренду свободной — выравниваем предфильтр с этим гейтом.
    Аккаунт с ЖИВОЙ арендой (op_lease_until в будущем) по-прежнему пропускаем:
    две сессии одного аккаунта = AUTH_KEY_DUPLICATED (бан)."""
    return await pool.fetch(
        """SELECT wp.*, a.owner_id, a.proxy_id, up.geo_country
           FROM account_warmup_plans wp
           JOIN tg_accounts a ON a.id = wp.account_id
           LEFT JOIN user_proxies up ON up.id = a.proxy_id
           WHERE wp.status = 'active'
             AND (COALESCE(a.in_operation, FALSE) = FALSE
                  OR a.op_lease_until IS NULL
                  OR a.op_lease_until < NOW())
             AND (wp.last_action_at IS NULL
                  OR wp.last_action_at < NOW() - INTERVAL '20 hours')""",
    )


async def run_warmup_loop(pool: asyncpg.Pool, interval_hours: int = 1, bot=None) -> None:
    """
    Фоновый цикл: каждый час проверяет активные планы И сессии разогрева.
    Один запуск в сутки на план/сессию (проверяем last_action_at > 20ч).
    Планы и сессии запускаются ПАРАЛЛЕЛЬНО (до _MAX_PARALLEL_WARMUP одновременно),
    но не более одного аккаунта на один proxy_id в одном батче.
    Smart scheduling: аккаунт не греется в СВОЮ локальную ночь (23:00–07:00 по
    таймзоне гео его прокси, см. geo_tempo), а не по серверному Киеву — иначе
    поведение рассинхронизировано с заявленным гео аккаунта.
    """
    while True:
        try:
            # Сначала — рассказать о планах, которые движок остановил сам
            # (бан/спам-блок/длинный flood). Раньше это состояние было немым.
            if bot is not None:
                try:
                    await notify_paused_plans(pool, bot)
                except Exception:
                    log.debug("warmup loop: уведомления о паузах не отправлены")

            # Ночь считаем по ЛОКАЛЬНОМУ времени КАЖДОГО аккаунта (гео его прокси),
            # а не по серверному Киеву. Раньше весь цикл замирал в киевскую ночь:
            # US-аккаунты простаивали в свой вечер (естественно активное время), а
            # азиатские грелись в свою ночь — рассинхрон гео и поведения.
            from services import geo_tempo

            # Одиночные планы разогрева — включаем proxy_id для proxy-correlation
            # и geo_country прокси для локального времени аккаунта.
            rows = await _select_due_plans(pool)
            if rows:
                _total = len(rows)
                rows = [r for r in rows if not geo_tempo.is_local_night(r["geo_country"])]
                if _total != len(rows):
                    log.info(
                        "warmup loop: %d/%d планов отложено — у аккаунта локальная ночь",
                        _total - len(rows), _total,
                    )
            if rows:
                log.info("warmup loop: %d single-plans to run (proxy-safe batches)", len(rows))
            for batch in _proxy_safe_batches(list(rows), _MAX_PARALLEL_WARMUP):
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

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("warmup loop error: %s", e)
        await asyncio.sleep(interval_hours * 3600)
