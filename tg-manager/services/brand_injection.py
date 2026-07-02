"""Brand injection — нативная реклама @MEXAHI3MBOT в инфраструктуре free-tier пользователей.

Стратегия:
- Тексты постов/рассылок: вращающийся набор компеллинг-вариантов (не статичный суффикс)
- Описания каналов: plain-text mention (255 char limit)
- При создании канала: welcome-пост + pin (постоянный billboard)
- @MEXAHI3MBOT как admin во всех каналах
- Free-tier check по bot_id (для managed bots) и по user_id (для Telethon-операций)
"""
from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

PROMO_USERNAME = "MEXAHI3MBOT"
PROMO_URL = f"https://t.me/{PROMO_USERNAME}"

# ── Вращающиеся варианты для постов в каналах/группах (HTML) ─────────────────
_CHANNEL_VARIANTS = [
    f'\n\n🤖 Автоматизация через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a>',
    f'\n\n⚡ Канал работает через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a> — автоматизация Telegram',
    f'\n\n📡 Опубликовано через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a>',
    f'\n\n🚀 Автоматизация через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a> — Telegram OS',
    f'\n\n📊 Контент публикуется автоматически через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a>',
]

# ── Вращающиеся варианты для рассылок в managed-ботах (HTML) ─────────────────
_BROADCAST_VARIANTS = [
    f'\n\n🤖 Автоматизация через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a>',
    f'\n\n📨 Бот работает через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a>',
    f'\n\n⚡ Автоматизация через <a href="{PROMO_URL}">@{PROMO_USERNAME}</a>',
]

# ── Вращающиеся варианты для DM (plain text, без HTML) ───────────────────────
_DM_VARIANTS = [
    f'\n\n🤖 Автоматизация через @{PROMO_USERNAME}',
    f'\n\n📨 Отправлено через @{PROMO_USERNAME}',
    f'\n\n⚡ Автоматизация через @{PROMO_USERNAME}',
]

# ── Описание канала (plain text, ≤ 255 символов) ─────────────────────────────
_DESCRIPTION_SUFFIX = f"\n🤖 @{PROMO_USERNAME}"
_DESCRIPTION_MAX = 255

# ── In-memory кэши планов ─────────────────────────────────────────────────────
_plan_cache: dict[int, bool] = {}       # bot_id → is_free_tier
_user_plan_cache: dict[int, bool] = {}  # user_id → is_free_tier


def _pick(variants: list[str], text: str) -> str:
    """Детерминированно выбрать вариант по длине текста."""
    return variants[len(text) % len(variants)]


def add_promo(text: str, html: bool = True, context: str = "channel") -> str:
    """Добавить брендинг к тексту, если его ещё нет.

    context: "channel" | "broadcast" | "dm"
    html: True для HTML parse_mode, False для plain text / DM
    """
    if PROMO_USERNAME in (text or ""):
        return text
    t = text or ""
    if not html or context == "dm":
        return t + _pick(_DM_VARIANTS, t)
    if context == "broadcast":
        return t + _pick(_BROADCAST_VARIANTS, t)
    return t + _pick(_CHANNEL_VARIANTS, t)


def add_promo_to_description(text: str) -> str:
    """Добавить mention в описание канала/группы (plain text, лимит 255 символов).

    Если места не хватает — не добавляет (не обрезает контент пользователя).
    """
    t = text or ""
    if PROMO_USERNAME in t:
        return t
    combined = t + _DESCRIPTION_SUFFIX
    if len(combined) <= _DESCRIPTION_MAX:
        return combined
    return t  # нет места — не трогаем


# ── Проверка плана ────────────────────────────────────────────────────────────

async def is_free_tier(pool: asyncpg.Pool, bot_id: int) -> bool:
    """True если владелец бота на free-тарифе (брендинг применяется)."""
    if bot_id in _plan_cache:
        return _plan_cache[bot_id]
    try:
        owner = await pool.fetchval(
            "SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id
        )
        if not owner:
            return False
        # Авторитетный источник плана — get_plan (таблица subscriptions), а не
        # platform_users.current_plan, который может отставать → иначе платящему
        # клиенту вставляется промо-брендинг.
        from bot.utils.subscription import get_plan, coerce_plan
        plan = coerce_plan(await get_plan(pool, owner))
        result = plan == "free"
        _plan_cache[bot_id] = result
        return result
    except Exception as e:
        log.debug("brand_injection.is_free_tier bot_id=%d: %s", bot_id, e)
        return False


async def is_user_free_tier(pool: asyncpg.Pool, user_id: int) -> bool:
    """True если пользователь на free-тарифе (Telethon-операции по user_id)."""
    if user_id in _user_plan_cache:
        return _user_plan_cache[user_id]
    try:
        from bot.utils.subscription import get_plan, coerce_plan
        plan = coerce_plan(await get_plan(pool, user_id))
        result = plan == "free"
        _user_plan_cache[user_id] = result
        return result
    except Exception as e:
        log.debug("brand_injection.is_user_free_tier user_id=%d: %s", user_id, e)
        return False


def invalidate_cache(bot_id: int | None = None, user_id: int | None = None) -> None:
    """Вызвать после апгрейда тарифа — сбрасывает кэш."""
    if bot_id is None and user_id is None:
        _plan_cache.clear()
        _user_plan_cache.clear()
    else:
        if bot_id is not None:
            _plan_cache.pop(bot_id, None)
        if user_id is not None:
            _user_plan_cache.pop(user_id, None)


# ── Канальные операции ────────────────────────────────────────────────────────

async def add_botmother_as_channel_admin(
    client,
    channel_id: int,
    access_hash: int = 0,
) -> bool:
    """Добавить @MEXAHI3MBOT как admin в канале/группе. Не выбрасывает исключений."""
    try:
        from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
        from telethon.tl.types import ChatAdminRights, InputChannel

        bot_entity = await client.get_input_entity(PROMO_USERNAME)
        channel = InputChannel(channel_id=channel_id, access_hash=access_hash)

        try:
            await client(InviteToChannelRequest(channel=channel, users=[bot_entity]))
        except Exception:
            pass

        rights = ChatAdminRights(
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=True,
            manage_call=True,
            other=True,
            change_info=True,
            anonymous=False,
            manage_topics=False,
        )
        await client(
            EditAdminRequest(
                channel=channel,
                user_id=bot_entity,
                admin_rights=rights,
                rank="Infragram",
            )
        )
        log.info("brand_injection: @%s promoted to admin in channel %d", PROMO_USERNAME, channel_id)
        return True
    except Exception as e:
        log.debug("brand_injection.add_botmother_as_channel_admin channel=%d: %s", channel_id, e)
        return False


async def post_welcome_and_pin(
    client,
    channel_id: int,
    access_hash: int = 0,
) -> bool:
    """Опубликовать брендированный welcome-пост и закрепить его в новом канале.

    Создаёт постоянный рекламный billboard при первом создании канала.
    Клиент Telethon должен быть уже подключён.
    """
    try:
        from telethon.tl.functions.messages import UpdatePinnedMessageRequest
        from telethon.tl.types import InputChannel

        text = (
            f'📣 <b>Этот канал создан и управляется через <a href="{PROMO_URL}">Infragram</a></b>\n\n'
            f'🔧 <b>Infragram</b> — Telegram OS: автоматизация каналов, ботов и аудитории '
            f'в единой инфраструктуре.\n\n'
            f'📈 Публикация • Рассылки • DM-кампании • Аналитика • Strike\n\n'
            f'🚀 Попробуй бесплатно: @{PROMO_USERNAME}'
        )
        channel = InputChannel(channel_id=channel_id, access_hash=access_hash)
        msg = await client.send_message(channel, text, parse_mode="html")
        await client(UpdatePinnedMessageRequest(peer=channel, id=msg.id, silent=True))
        log.info("brand_injection: welcome post pinned in channel %d", channel_id)
        return True
    except Exception as e:
        log.debug("brand_injection.post_welcome_and_pin channel=%d: %s", channel_id, e)
        return False
