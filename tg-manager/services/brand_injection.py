"""Brand injection — appends @MEXAHI3MBOT promo to free-tier bot messages.

Free-tier bots: branding appended to all outgoing text messages.
Paid-tier bots: no injection.
Channels/groups: @MEXAHI3MBOT added as admin on creation.
"""
from __future__ import annotations

import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

PROMO_USERNAME = "MEXAHI3MBOT"
# Plain-text variant (no HTML) for when parse_mode is unknown
PROMO_PLAIN = f"\n\n🤖 @{PROMO_USERNAME}"
# HTML variant for HTML parse_mode messages
PROMO_HTML = f'\n\n🤖 <a href="https://t.me/{PROMO_USERNAME}">@{PROMO_USERNAME}</a>'

# In-memory plan cache {bot_id: True/False}; refreshes on miss
_plan_cache: dict[int, bool] = {}
# In-memory user plan cache {user_id: True/False}
_user_plan_cache: dict[int, bool] = {}
_cache_hits = 0


def add_promo(text: str, html: bool = True) -> str:
    """Append branding to text if not already present."""
    tag = PROMO_USERNAME
    if tag in (text or ""):
        return text
    suffix = PROMO_HTML if html else PROMO_PLAIN
    return (text or "") + suffix


async def is_free_tier(pool: asyncpg.Pool, bot_id: int) -> bool:
    """Return True if the bot's owner is on free tier (branding should be applied)."""
    if bot_id in _plan_cache:
        return _plan_cache[bot_id]
    try:
        row = await pool.fetchrow(
            """SELECT pu.current_plan
               FROM managed_bots mb
               JOIN platform_users pu ON pu.user_id = mb.added_by
               WHERE mb.bot_id = $1""",
            bot_id,
        )
        plan = (row["current_plan"] if row else "free") or "free"
        result = plan.lower() in ("free", "")
        _plan_cache[bot_id] = result
        return result
    except Exception as e:
        log.debug("brand_injection.is_free_tier bot_id=%d: %s", bot_id, e)
        return False  # on error — don't inject (safe default)


async def is_user_free_tier(pool: asyncpg.Pool, user_id: int) -> bool:
    """Return True if the user (by Telegram user_id) is on free tier."""
    if user_id in _user_plan_cache:
        return _user_plan_cache[user_id]
    try:
        row = await pool.fetchrow(
            "SELECT current_plan FROM platform_users WHERE user_id=$1",
            user_id,
        )
        plan = (row["current_plan"] if row else "free") or "free"
        result = plan.lower() in ("free", "")
        _user_plan_cache[user_id] = result
        return result
    except Exception as e:
        log.debug("brand_injection.is_user_free_tier user_id=%d: %s", user_id, e)
        return False


def invalidate_cache(bot_id: int | None = None) -> None:
    """Call after plan upgrade to clear cached result."""
    if bot_id is None:
        _plan_cache.clear()
        _user_plan_cache.clear()
    else:
        _plan_cache.pop(bot_id, None)


async def add_botmother_as_channel_admin(
    client,  # Telethon TelegramClient, already connected
    channel_id: int,
    access_hash: int = 0,
) -> bool:
    """Promote @MEXAHI3MBOT to admin in a channel/group.

    Silently returns False on any failure — never raises.
    The Telethon client must already be connected.
    """
    try:
        from telethon.tl.functions.channels import EditAdminRequest, InviteToChannelRequest
        from telethon.tl.types import ChatAdminRights, InputChannel

        bot_entity = await client.get_input_entity(PROMO_USERNAME)

        channel = InputChannel(channel_id=channel_id, access_hash=access_hash)

        # Try to add bot to channel first (required before promoting)
        try:
            await client(InviteToChannelRequest(channel=channel, users=[bot_entity]))
        except Exception:
            pass  # Already a member or can't add — try promote anyway

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
                rank="BotMother",
            )
        )
        log.info("brand_injection: @%s promoted to admin in channel %d", PROMO_USERNAME, channel_id)
        return True
    except Exception as e:
        log.debug("brand_injection.add_botmother_as_channel_admin channel=%d: %s", channel_id, e)
        return False
