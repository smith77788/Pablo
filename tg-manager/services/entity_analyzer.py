"""
Comprehensive Telegram entity analyzer.

Fetches all available data for users, bots, channels, supergroups, groups
via Telethon: creation date, stats, content analysis, admins, network links,
SEO score, engagement metrics. Caches results to DB.

More detailed than Telelog/Funstat.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Union

import asyncpg

log = logging.getLogger(__name__)

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)

# ── OSINT constants ───────────────────────────────────────────────────────────

# IDs ≥ 5 trillion belong to Fragment anonymous number accounts (NFT-based)
_FRAGMENT_THRESHOLD = 5_000_000_000_000

_DC_REGIONS: dict[int, str] = {
    1: "DC1 🇺🇸 США (Вирджиния)",
    2: "DC2 🇳🇱 Нидерланды (Амстердам)",
    3: "DC3 🇺🇸 США (Майами)",
    4: "DC4 🇳🇱 Нид. / Азия",
    5: "DC5 🇸🇬 Сингапур / 🇦🇪 БВ",
}

_MTPROTO_ERROR_MAP: dict[str, str] = {
    "ChannelPrivateError": "Приватный канал",
    "ChatAdminRequiredError": "Нет прав администратора",
    "UserPrivacyRestrictedError": "Скрыто настройками приватности",
    "PeerIdInvalidError": "Неверный ID",
    "FloodWaitError": "Превышен лимит запросов (FloodWait)",
    "UsernameInvalidError": "Неверный username",
    "UsernameNotOccupiedError": "Username не занят",
    "AuthKeyUnregisteredError": "Сессия недействительна",
    "RPCError": "Ошибка MTProto RPC",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _age_str(dt: datetime) -> str:
    days = (datetime.now(tz=timezone.utc) - dt).days
    y, rem = divmod(days, 365)
    m = rem // 30
    parts = []
    if y:
        parts.append(f"{y} г.")
    if m:
        parts.append(f"{m} мес.")
    if not parts:
        parts.append(f"{days} дн.")
    return " ".join(parts)


def _num(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}М"
    if n >= 1_000:
        return f"{n/1_000:.1f}К"
    return str(n)


def _pct(a: float, b: float) -> str:
    if not b:
        return "—"
    return f"{a/b*100:.1f}%"


def _bar(val: float, max_val: float, width: int = 8) -> str:
    if not max_val:
        return "░" * width
    filled = round(width * min(val / max_val, 1.0))
    return "█" * filled + "░" * (width - filled)


# ── Core analysis ─────────────────────────────────────────────────────────────

async def _get_client(pool: asyncpg.Pool, owner_id: int):
    """Return first connected client from account pool (resilient: tries all)."""
    from services import resource_selector
    from services.account_manager import _make_client

    candidates = await resource_selector.select_all_active(pool, owner_id, action_type="read")
    for acc in candidates:
        if not acc.get("session_str"):
            continue
        client = _make_client(acc["session_str"])
        try:
            await asyncio.wait_for(client.connect(), timeout=12)
            return client
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
    return None


# ── OSINT helper functions ─────────────────────────────────────────────────────

def _is_fragment_number(entity_id: int) -> bool:
    """Fragment anonymous number accounts have IDs ≥ 5 trillion."""
    return entity_id >= _FRAGMENT_THRESHOLD


def _extract_dc(entity) -> int | None:
    """Extract DC ID from entity's photo metadata (peer.photo.dc_id)."""
    photo = getattr(entity, "photo", None)
    if photo is None:
        return None
    dc = getattr(photo, "dc_id", None)
    return int(dc) if dc is not None else None


async def _get_avatar_metrics(client, entity) -> dict[str, Any]:
    """
    Extract avatar lifecycle metrics: total count + oldest photo ID.
    The photo object's .id behaves linearly within server clusters
    and can serve as secondary epoch verification.
    """
    total = 0
    oldest_photo_id = None
    try:
        result = await asyncio.wait_for(
            client.get_profile_photos(entity, limit=100), timeout=15
        )
        photos = list(result)
        total = result.total if hasattr(result, "total") else len(photos)
        if photos:
            oldest = min(photos, key=lambda p: p.id)
            oldest_photo_id = oldest.id
    except Exception:
        pass
    return {"total_historical_count": total, "oldest_photo_id": oldest_photo_id}


async def _get_db_footprint(pool: asyncpg.Pool, entity_id: int) -> int | None:
    """
    Cross-reference our reg_check_cache for earliest sighting of this entity.
    Returns Unix timestamp or None if not previously tracked.
    """
    try:
        val = await pool.fetchval(
            "SELECT MIN(checked_at) FROM reg_check_cache WHERE entity_id=$1",
            entity_id,
        )
        return int(val.timestamp()) if val else None
    except Exception:
        return None


def _confidence_score(
    has_exact_date: bool,
    has_dc: bool,
    has_avatar: bool,
    has_db_footprint: bool,
    is_fragment: bool,
) -> float:
    """
    Confidence score 0.0–1.0 based on data alignment:
      0.30  base (ID interpolation always available)
    + 0.40  exact creation date via first message
    + 0.15  DC extracted (entity is reachable)
    + 0.10  avatar history available (secondary epoch signal)
    + 0.05  historical DB sighting
    Fragment numbers start at 0.10 (no meaningful date signal).
    """
    if is_fragment:
        return 0.10
    score = 0.30
    if has_exact_date:
        score += 0.40
    if has_dc:
        score += 0.15
    if has_avatar:
        score += 0.10
    if has_db_footprint:
        score += 0.05
    return round(min(score, 1.0), 2)


def _map_mtproto_error(exc: Exception) -> dict[str, Any]:
    """Map MTProto exceptions to structured metadata — never surface raw errors."""
    name = type(exc).__name__
    description = _MTPROTO_ERROR_MAP.get(name, str(exc)[:150])
    return {
        "error_type": name,
        "description": description,
        "privacy_restrictions": {
            "hidden_forward_link": name in ("UserPrivacyRestrictedError",),
            "private_chat": name in (
                "ChannelPrivateError", "ChatAdminRequiredError",
            ),
        },
    }


async def analyze_channel(
    pool: asyncpg.Pool,
    owner_id: int,
    peer,  # username str | Telethon entity
    post_sample: int = 50,
) -> dict[str, Any] | None:
    """
    Full channel/supergroup analysis. Returns structured dict with all metrics.
    post_sample: how many recent posts to analyze for content stats.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest
    from telethon.tl.types import (
        Channel, ChannelParticipantsAdmins, ChannelParticipantsBots,
        MessageReactions,
    )
    from services.registration_checker import estimate_by_id, get_channel_exact_date

    client = await _get_client(pool, owner_id)
    if not client:
        return None

    try:
        # ── Resolve entity ────────────────────────────────────────────────────
        entity = await asyncio.wait_for(client.get_entity(peer), timeout=20)
        if not isinstance(entity, Channel):
            return None

        # ── GetFullChannel ────────────────────────────────────────────────────
        full = await asyncio.wait_for(
            client(GetFullChannelRequest(entity)), timeout=20
        )
        fc = full.full_chat  # ChannelFull
        ch = entity           # Channel

        # ── Basic info ────────────────────────────────────────────────────────
        entity_type = "supergroup" if ch.megagroup else "channel"
        entity_id = ch.id
        title = ch.title or ""
        username = ch.username or None
        description = fc.about or ""
        members = fc.participants_count or 0
        admins_count = fc.admins_count or 0
        banned_count = fc.banned_count or 0
        online_count = getattr(fc, "online_count", None)
        linked_chat_id = getattr(fc, "linked_chat_id", None)
        slowmode = getattr(fc, "slowmode_next_send_date", None)
        slowmode_s = getattr(fc, "slowmode_seconds", 0) or 0
        ttl = getattr(fc, "ttl_period", None)
        noforwards = bool(getattr(ch, "noforwards", False))
        verified = bool(getattr(ch, "verified", False))
        scam = bool(getattr(ch, "scam", False))
        fake = bool(getattr(ch, "fake", False))
        restricted = bool(getattr(ch, "restricted", False))
        restriction_reason = getattr(ch, "restriction_reason", [])
        join_to_send = bool(getattr(ch, "join_to_send", False))
        join_request = bool(getattr(ch, "join_request", False))
        is_forum = bool(getattr(ch, "forum", False))
        is_gigagroup = bool(getattr(ch, "gigagroup", False))
        has_signatures = bool(getattr(ch, "signatures", False))
        boost_level = getattr(fc, "boost_level", 0) or 0

        # ── Registration date ─────────────────────────────────────────────────
        id_estimate = estimate_by_id(entity_id, entity_type)
        created_at = id_estimate["date"]

        # ── Admin + bot count ─────────────────────────────────────────────────
        bot_count = 0
        admin_list: list[dict] = []
        try:
            adm_result = await asyncio.wait_for(
                client(GetParticipantsRequest(entity, ChannelParticipantsAdmins(), 0, 50, 0)),
                timeout=15,
            )
            for u in adm_result.users[:20]:
                admin_list.append({
                    "id": u.id,
                    "name": (u.first_name or "") + (" " + u.last_name if u.last_name else ""),
                    "username": u.username,
                    "is_bot": u.bot,
                    "verified": getattr(u, "verified", False),
                })
            admins_count = max(admins_count, len(adm_result.participants))
        except Exception:
            pass

        try:
            bot_result = await asyncio.wait_for(
                client(GetParticipantsRequest(entity, ChannelParticipantsBots(), 0, 50, 0)),
                timeout=15,
            )
            bot_count = len(bot_result.participants)
        except Exception:
            pass

        # ── Recent posts analysis ─────────────────────────────────────────────
        views_list: list[int] = []
        fwd_list: list[int] = []
        react_list: list[int] = []
        reply_list: list[int] = []
        dates: list[datetime] = []
        hashtags: Counter = Counter()
        media_types: Counter = Counter()
        post_lengths: list[int] = []
        top_posts: list[dict] = []

        try:
            async for msg in client.iter_messages(entity, limit=post_sample):
                if msg.service or not msg.date:
                    continue
                v = msg.views or 0
                f = msg.forwards or 0
                r = 0
                if msg.reactions and hasattr(msg.reactions, "results"):
                    r = sum(rc.count for rc in msg.reactions.results)
                rpl = msg.replies.replies if msg.replies else 0

                views_list.append(v)
                fwd_list.append(f)
                react_list.append(r)
                reply_list.append(rpl)
                dates.append(msg.date)

                text = msg.text or msg.message or ""
                for tag in _HASHTAG_RE.findall(text):
                    hashtags[tag.lower()] += 1
                post_lengths.append(len(text))

                if msg.photo:
                    media_types["📷 Фото"] += 1
                elif msg.video:
                    media_types["🎥 Видео"] += 1
                elif msg.document:
                    media_types["📎 Файл"] += 1
                elif msg.audio:
                    media_types["🎵 Аудио"] += 1
                elif msg.poll:
                    media_types["📊 Опрос"] += 1
                elif text:
                    media_types["✏️ Текст"] += 1

                if v > 0 and len(top_posts) < 5:
                    top_posts.append({
                        "id": msg.id,
                        "views": v,
                        "reactions": r,
                        "text": (text[:80] + "…") if len(text) > 80 else text,
                        "date": msg.date,
                        "url": f"https://t.me/{username}/{msg.id}" if username else None,
                    })
        except Exception as e:
            log.debug("entity_analyzer post scan: %s", e)

        # Sort top posts by views
        top_posts.sort(key=lambda x: x["views"], reverse=True)

        # ── Derived metrics ───────────────────────────────────────────────────
        avg_views = int(sum(views_list) / len(views_list)) if views_list else 0
        avg_fwd = int(sum(fwd_list) / len(fwd_list)) if fwd_list else 0
        avg_react = int(sum(react_list) / len(react_list)) if react_list else 0
        avg_replies = int(sum(reply_list) / len(reply_list)) if reply_list else 0
        max_views = max(views_list) if views_list else 0

        # Engagement rate = (reactions + replies) / views
        er = (sum(react_list) + sum(reply_list)) / max(sum(views_list), 1) * 100

        # Post frequency (posts/day over the sample period)
        if len(dates) >= 2:
            span_days = (dates[0] - dates[-1]).total_seconds() / 86400
            posts_per_day = len(dates) / max(span_days, 1)
        else:
            posts_per_day = 0

        # Activity hours distribution (UTC)
        hour_dist: Counter = Counter()
        for d in dates:
            hour_dist[d.hour] += 1
        peak_hour = hour_dist.most_common(1)[0][0] if hour_dist else None

        # ── SEO score ─────────────────────────────────────────────────────────
        seo_score, seo_notes = _calc_seo(title, description, username, members, posts_per_day)

        # ── Linked entity name ────────────────────────────────────────────────
        linked_name = None
        if linked_chat_id:
            try:
                linked_ent = await asyncio.wait_for(
                    client.get_entity(linked_chat_id), timeout=10
                )
                linked_name = getattr(linked_ent, "title", None) or getattr(linked_ent, "username", None)
            except Exception:
                pass

        # ── OSINT enrichment ──────────────────────────────────────────────────
        dc_id = _extract_dc(ch)
        is_frag = _is_fragment_number(entity_id)
        footprint = await _get_db_footprint(pool, entity_id)

        # Try deep channel creation date via GetHistoryRequest (first message)
        exact_date: datetime | None = None
        try:
            from telethon.tl.functions.messages import GetHistoryRequest
            history = await asyncio.wait_for(
                client(GetHistoryRequest(
                    peer=ch,
                    offset_id=2,
                    offset_date=None,
                    add_offset=-1,
                    limit=1,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )),
                timeout=15,
            )
            if history.messages:
                exact_date = history.messages[0].date
        except Exception:
            pass
        if not exact_date:
            try:
                msg = await asyncio.wait_for(
                    client.get_messages(ch, ids=1), timeout=15
                )
                if msg and hasattr(msg, "date") and msg.date:
                    exact_date = msg.date
                elif msg and isinstance(msg, list) and msg and getattr(msg[0], "date", None):
                    exact_date = msg[0].date
            except Exception:
                pass
        if exact_date:
            created_at = exact_date

        confidence = _confidence_score(
            has_exact_date=exact_date is not None,
            has_dc=dc_id is not None,
            has_avatar=False,
            has_db_footprint=footprint is not None,
            is_fragment=is_frag,
        )
        recon_payload: dict[str, Any] = {
            "object_type": entity_type,
            "dc_id": dc_id,
            "is_fragment_number": is_frag,
            "estimated_creation_timestamp": int(id_estimate["date"].timestamp()),
            "exact_creation_timestamp": int(exact_date.timestamp()) if exact_date else None,
            "avatar_metrics": {"total_historical_count": 0, "oldest_photo_id": None},
            "first_spotted_in_our_db": footprint,
            "confidence_score": confidence,
            "privacy_restrictions": {
                "hidden_forward_link": noforwards,
                "private_chat": username is None,
            },
        }

        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "title": title,
            "username": username,
            "description": description,
            "members": members,
            "admins_count": admins_count,
            "bot_count": bot_count,
            "banned_count": banned_count,
            "online_count": online_count,
            "boost_level": boost_level,
            "created_at": created_at,
            "created_method": "first_message" if exact_date else id_estimate["method"],
            "exact_date": exact_date,
            "linked_chat_id": linked_chat_id,
            "linked_name": linked_name,
            "slowmode_s": slowmode_s,
            "ttl": ttl,
            "noforwards": noforwards,
            "verified": verified,
            "scam": scam,
            "fake": fake,
            "restricted": restricted,
            "restriction_reason": [str(r) for r in restriction_reason],
            "join_to_send": join_to_send,
            "join_request": join_request,
            "is_forum": is_forum,
            "is_gigagroup": is_gigagroup,
            "has_signatures": has_signatures,
            # content
            "avg_views": avg_views,
            "avg_fwd": avg_fwd,
            "avg_react": avg_react,
            "avg_replies": avg_replies,
            "max_views": max_views,
            "engagement_rate": round(er, 2),
            "posts_per_day": round(posts_per_day, 2),
            "peak_hour": peak_hour,
            "top_hashtags": hashtags.most_common(10),
            "media_types": dict(media_types.most_common()),
            "avg_post_length": int(sum(post_lengths) / len(post_lengths)) if post_lengths else 0,
            "top_posts": top_posts[:5],
            "hour_dist": dict(hour_dist),
            # admins
            "admin_list": admin_list,
            # seo
            "seo_score": seo_score,
            "seo_notes": seo_notes,
            # sample size
            "posts_analyzed": len(views_list),
            # OSINT
            "dc_id": dc_id,
            "is_fragment_number": is_frag,
            "confidence_score": confidence,
            "first_spotted_in_our_db": footprint,
            "recon_payload": recon_payload,
        }

    except asyncio.TimeoutError:
        log.warning("entity_analyzer: timeout for %s", peer)
        return None
    except Exception as e:
        log.warning("entity_analyzer.analyze_channel(%s): %s — %s",
                    peer, type(e).__name__, e)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def analyze_user(
    pool: asyncpg.Pool,
    owner_id: int,
    peer,
) -> dict[str, Any] | None:
    """Full user/bot analysis with OSINT enrichment."""
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.types import User
    from services.registration_checker import estimate_by_id

    client = await _get_client(pool, owner_id)
    if not client:
        return None

    try:
        entity = await asyncio.wait_for(client.get_entity(peer), timeout=20)
        if not isinstance(entity, User):
            return None

        full_result = await asyncio.wait_for(
            client(GetFullUserRequest(entity)), timeout=20
        )
        fu = full_result.full_user
        u = entity

        entity_type = "bot" if u.bot else "user"
        entity_id = u.id
        name = ((u.first_name or "") + (" " + u.last_name if u.last_name else "")).strip()
        username = u.username
        bio = fu.about or ""
        common_groups = getattr(fu, "common_chats_count", 0) or 0
        premium = bool(getattr(u, "premium", False))
        verified = bool(getattr(u, "verified", False))
        scam = bool(getattr(u, "scam", False))
        fake = bool(getattr(u, "fake", False))
        restricted = bool(getattr(u, "restricted", False))
        phone = getattr(u, "phone", None)
        is_contact = bool(getattr(u, "contact", False))
        is_mutual = bool(getattr(u, "mutual_contact", False))
        noforwards = bool(getattr(u, "noforwards", False))

        # ── Avatar lifecycle metrics (Method C) ───────────────────────────────
        avatar_met = await _get_avatar_metrics(client, u)
        photos_count = avatar_met["total_historical_count"]

        # Bot-specific info
        bot_info: dict = {}
        if u.bot:
            bi = getattr(fu, "bot_info", None)
            if bi:
                bot_info["description"] = getattr(bi, "description", "") or ""
                bot_info["commands"] = [
                    {"cmd": c.command, "desc": c.description}
                    for c in (getattr(bi, "commands", None) or [])
                ]
                bot_info["privacy_url"] = getattr(bi, "privacy_policy_url", None)
            bot_info["no_chats"] = bool(getattr(u, "bot_nochats", False))
            bot_info["inline_geo"] = bool(getattr(u, "bot_inline_geo", False))
            bot_info["inline_placeholder"] = getattr(u, "bot_inline_placeholder", None)
            bot_info["history_access"] = bool(getattr(u, "bot_chat_history", False))
            bot_info["attach_menu"] = bool(getattr(u, "bot_attach_menu", False))

        # Status string
        status_str = "неизвестен"
        st = getattr(u, "status", None)
        if st:
            sn = type(st).__name__
            if sn == "UserStatusOnline":
                status_str = "🟢 онлайн"
            elif sn == "UserStatusOffline":
                was = getattr(st, "was_online", None)
                if was:
                    status_str = f"был(а) {_ago(was)}"
                else:
                    status_str = "⚫ оффлайн"
            elif sn == "UserStatusRecently":
                status_str = "🟡 был(а) недавно"
            elif sn == "UserStatusLastWeek":
                status_str = "был(а) на этой неделе"
            elif sn == "UserStatusLastMonth":
                status_str = "был(а) в этом месяце"

        id_estimate = estimate_by_id(entity_id, entity_type)

        # ── OSINT enrichment ──────────────────────────────────────────────────
        dc_id = _extract_dc(u)
        is_frag = _is_fragment_number(entity_id)
        footprint = await _get_db_footprint(pool, entity_id)
        confidence = _confidence_score(
            has_exact_date=False,
            has_dc=dc_id is not None,
            has_avatar=avatar_met["total_historical_count"] > 0,
            has_db_footprint=footprint is not None,
            is_fragment=is_frag,
        )
        recon_payload: dict[str, Any] = {
            "object_type": entity_type,
            "dc_id": dc_id,
            "is_fragment_number": is_frag,
            "estimated_creation_timestamp": int(id_estimate["date"].timestamp()),
            "exact_creation_timestamp": None,
            "avatar_metrics": avatar_met,
            "first_spotted_in_our_db": footprint,
            "confidence_score": confidence,
            "privacy_restrictions": {
                "hidden_forward_link": noforwards,
                "private_chat": username is None,
            },
        }

        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "name": name,
            "username": username,
            "bio": bio,
            "phone": phone,
            "premium": premium,
            "verified": verified,
            "scam": scam,
            "fake": fake,
            "restricted": restricted,
            "is_contact": is_contact,
            "is_mutual": is_mutual,
            "common_groups": common_groups,
            "photos_count": photos_count,
            "status": status_str,
            "created_at": id_estimate["date"],
            "created_method": id_estimate["method"],
            "bot_info": bot_info,
            # OSINT
            "dc_id": dc_id,
            "is_fragment_number": is_frag,
            "avatar_metrics": avatar_met,
            "confidence_score": confidence,
            "first_spotted_in_our_db": footprint,
            "recon_payload": recon_payload,
        }

    except asyncio.TimeoutError:
        log.warning("entity_analyzer.analyze_user: timeout for %s", peer)
        return None
    except Exception as e:
        log.warning("entity_analyzer.analyze_user(%s): %s — %s",
                    peer, type(e).__name__, e)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def analyze_telegram_object(
    pool: asyncpg.Pool,
    owner_id: int,
    target: Union[str, int],
) -> dict[str, Any]:
    """
    Enterprise unified OSINT entry point.
    Routes to analyze_channel or analyze_user depending on resolved entity type.
    Returns enriched dict with 'recon_payload' conforming to the standard schema.
    Falls back to ID-only estimation when Telethon is unavailable.
    """
    from telethon.tl.types import User, Channel, Chat
    from services.registration_checker import estimate_by_id, canonical_peer_id

    client = await _get_client(pool, owner_id)
    if not client:
        # Partial result: ID interpolation only
        if isinstance(target, int):
            raw_id = target
            if raw_id < -1_000_000_000:
                etype = "channel"
            elif raw_id < 0:
                etype = "group"
            else:
                etype = "user"
            eid = canonical_peer_id(raw_id)
            est = estimate_by_id(eid, etype)
            is_frag = _is_fragment_number(eid)
            footprint = await _get_db_footprint(pool, eid)
            confidence = 0.10 if is_frag else 0.30
            return {
                "_partial": True,
                "entity_id": eid,
                "entity_type": etype,
                "created_at": est["date"],
                "created_method": "id_interpolation",
                "recon_payload": {
                    "object_type": etype,
                    "dc_id": None,
                    "is_fragment_number": is_frag,
                    "estimated_creation_timestamp": int(est["date"].timestamp()),
                    "exact_creation_timestamp": None,
                    "avatar_metrics": {"total_historical_count": 0, "oldest_photo_id": None},
                    "first_spotted_in_our_db": footprint,
                    "confidence_score": confidence,
                    "privacy_restrictions": {"hidden_forward_link": False, "private_chat": False},
                },
            }
        return {"_partial": True}

    try:
        entity = await asyncio.wait_for(client.get_entity(target), timeout=20)
    except Exception as exc:
        await client.disconnect()
        err_meta = _map_mtproto_error(exc)
        # Still return ID-based partial if we have an int target
        if isinstance(target, int):
            eid = canonical_peer_id(abs(target))
            etype = "channel" if target < -1_000_000_000 else "user"
            est = estimate_by_id(eid, etype)
            is_frag = _is_fragment_number(eid)
            footprint = await _get_db_footprint(pool, eid)
            return {
                "_partial": True,
                "_error_meta": err_meta,
                "entity_id": eid,
                "entity_type": etype,
                "created_at": est["date"],
                "created_method": "id_interpolation",
                "recon_payload": {
                    "object_type": etype,
                    "dc_id": None,
                    "is_fragment_number": is_frag,
                    "estimated_creation_timestamp": int(est["date"].timestamp()),
                    "exact_creation_timestamp": None,
                    "avatar_metrics": {"total_historical_count": 0, "oldest_photo_id": None},
                    "first_spotted_in_our_db": footprint,
                    "confidence_score": 0.15,
                    "privacy_restrictions": err_meta.get("privacy_restrictions", {}),
                },
            }
        return {"_partial": True, "_error_meta": err_meta}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # Route to the appropriate analyzer
    if isinstance(entity, User):
        peer = entity.username or entity.id
        return await analyze_user(pool, owner_id, peer) or {"_partial": True}
    elif isinstance(entity, (Channel, Chat)):
        peer = getattr(entity, "username", None) or entity.id
        return await analyze_channel(pool, owner_id, peer) or {"_partial": True}

    return {"_partial": True}


def _ago(dt: datetime) -> str:
    now = datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    if diff.total_seconds() < 60:
        return "только что"
    if diff.total_seconds() < 3600:
        return f"{int(diff.total_seconds()/60)} мин. назад"
    if diff.days == 0:
        return f"{int(diff.total_seconds()/3600)} ч. назад"
    if diff.days < 30:
        return f"{diff.days} дн. назад"
    return f"{diff.days//30} мес. назад"


def _calc_seo(title: str, desc: str, username: str | None, members: int, ppd: float) -> tuple[int, list[str]]:
    """Calculate SEO score 0-100 with improvement notes."""
    score = 0
    notes: list[str] = []

    # Title: 3-25 chars optimal
    tl = len(title)
    if 5 <= tl <= 25:
        score += 20
    elif tl > 0:
        score += 10
        if tl < 5:
            notes.append("⚠️ Название слишком короткое (менее 5 символов)")
        elif tl > 25:
            notes.append("⚠️ Название длинное (>25 символов) — может обрезаться в поиске")

    # Description: 70-160 chars optimal
    dl = len(desc)
    if 70 <= dl <= 160:
        score += 20
    elif 20 <= dl < 70:
        score += 10
        notes.append("💡 Описание можно расширить (оптимально 70-160 символов)")
    elif dl == 0:
        notes.append("❌ Нет описания — критично для поиска")
    else:
        score += 5
        notes.append("⚠️ Описание слишком длинное (>160 символов) — поиск видит только первые 160")

    # Username: having it + format
    if username:
        score += 20
        ulen = len(username)
        if ulen < 5:
            notes.append("⚠️ Username слишком короткий")
        elif ulen > 20:
            notes.append("💡 Username длинный — рассмотрите сокращение")
        if "_" in username:
            notes.append("💡 Username с _ — слова без _ лучше ранжируются")
    else:
        notes.append("❌ Нет публичного username — канал не найти в поиске")

    # Keywords in title+desc
    words = len((title + " " + desc).split())
    if words >= 20:
        score += 15
    elif words >= 10:
        score += 8
    else:
        notes.append("💡 Добавьте больше ключевых слов в название и описание")

    # Post frequency
    if ppd >= 1:
        score += 15
    elif ppd >= 0.5:
        score += 8
        notes.append("💡 Публикуйте чаще (минимум 1 пост/день для лучшего ранжирования)")
    elif ppd > 0:
        score += 3
        notes.append("⚠️ Очень редкие публикации ухудшают позиции в поиске")
    else:
        notes.append("❌ Нет публикаций — канал не ранжируется")

    # Members (social proof)
    if members >= 10_000:
        score += 10
    elif members >= 1_000:
        score += 6
    elif members >= 100:
        score += 3

    return min(score, 100), notes


# ── Formatters ────────────────────────────────────────────────────────────────

def format_overview(data: dict) -> str:
    et = data.get("entity_type", "")
    icons = {"channel": "📢", "supergroup": "👥", "user": "👤", "bot": "🤖", "group": "👥"}
    icon = icons.get(et, "❓")
    labels = {"channel": "Канал", "supergroup": "Супергруппа", "user": "Пользователь", "bot": "Бот"}
    label = labels.get(et, et.capitalize())

    title = html.escape(data.get("title") or data.get("name") or "")
    uname = data.get("username")
    badges = []
    if data.get("verified"):
        badges.append("✅ Верифицирован")
    if data.get("premium"):
        badges.append("⭐ Premium")
    if data.get("scam"):
        badges.append("🚫 SCAM")
    if data.get("fake"):
        badges.append("⚠️ FAKE")
    if data.get("restricted"):
        badges.append("🔒 Ограничен")
    if data.get("is_fragment_number"):
        badges.append("🔷 Fragment NFT")

    lines = [f"{icon} <b>{label}</b>  {' '.join(badges)}"]
    if title:
        lines.append(f"🏷 <b>{html.escape(title)}</b>")
    if uname:
        lines.append(f"🔗 @{uname}  →  t.me/{uname}")

    eid = data.get("entity_id", 0)
    lines.append(f"🆔 <code>{eid}</code>")

    # ── DC & OSINT block ──────────────────────────────────────────────────────
    dc_id = data.get("dc_id")
    if dc_id is not None:
        dc_label = _DC_REGIONS.get(dc_id, f"DC{dc_id}")
        lines.append(f"📡 Датацентр: <b>{dc_label}</b>")

    if data.get("is_fragment_number"):
        lines.append("🔷 <b>Fragment анонимный номер</b> — дата не определяется")
    else:
        ct = data.get("created_at")
        if ct:
            from services.registration_checker import format_date_ru, format_age
            lines.append(f"\n📅 Создан: <b>{format_date_ru(ct)}</b>")
            lines.append(f"⏳ Возраст: <b>{format_age(ct)}</b>")
            method = data.get("created_method", "id_interpolation")
            if method == "first_message":
                lines.append("🎯 Метод: первое сообщение (точно)")
            else:
                lines.append("📊 Метод: оценка по ID (±2 мес.)")

    # Confidence score
    conf = data.get("confidence_score")
    if conf is not None:
        pct = int(conf * 100)
        bar_w = 8
        filled = round(bar_w * conf)
        bar = "█" * filled + "░" * (bar_w - filled)
        lines.append(f"🎯 Достоверность: [{bar}] <b>{pct}%</b>")

    # Avatar metrics
    av = data.get("avatar_metrics") or {}
    total_ph = av.get("total_historical_count", 0)
    oldest_ph = av.get("oldest_photo_id")
    if et in ("user", "bot") and total_ph > 0:
        ph_line = f"🖼 Фото в истории: <b>{total_ph}</b>"
        if oldest_ph:
            ph_line += f"  (oldest ID: <code>{oldest_ph}</code>)"
        lines.append(ph_line)

    # DB footprint
    footprint = data.get("first_spotted_in_our_db")
    if footprint:
        from datetime import datetime, timezone
        ft = datetime.fromtimestamp(footprint, tz=timezone.utc)
        from services.registration_checker import format_date_ru
        lines.append(f"🗄 Первое обнаружение в БД: <b>{format_date_ru(ft)}</b>")

    if et in ("channel", "supergroup"):
        m = data.get("members", 0)
        lines.append(f"\n👥 Подписчики: <b>{_num(m)}</b>")
        bl = data.get("boost_level", 0)
        if bl:
            lines.append(f"🚀 Уровень буста: <b>{bl}</b>")

        desc = data.get("description", "")
        if desc:
            short = desc[:200] + ("…" if len(desc) > 200 else "")
            lines.append(f"\n📝 <i>{html.escape(short)}</i>")

        flags = []
        if data.get("noforwards"):
            flags.append("🚫 Пересылка запрещена")
        if data.get("is_forum"):
            flags.append("💬 Форум (темы)")
        if data.get("is_gigagroup"):
            flags.append("📡 Гигагруппа")
        if data.get("has_signatures"):
            flags.append("✍️ Подписи авторов")
        if data.get("join_request"):
            flags.append("📋 Вступление по заявке")
        sl = data.get("slowmode_s", 0)
        if sl:
            flags.append(f"🐌 Медленный режим: {sl}с")
        ttl_val = data.get("ttl")
        if ttl_val:
            flags.append(f"⏱ Автоудаление: {ttl_val}с")
        if flags:
            lines.append("\n" + " · ".join(flags))

        ln = data.get("linked_name")
        if ln:
            lines.append(f"🔗 Связан с: <b>{html.escape(ln)}</b>")

    elif et in ("user", "bot"):
        bio = data.get("bio", "")
        if bio:
            lines.append(f"\n📝 <i>{html.escape(bio[:200])}</i>")
        lines.append(f"\n🔔 Статус: {data.get('status', '—')}")
        if not av:
            # Fallback for old data without avatar_metrics
            lines.append(f"🖼 Фото профиля: {data.get('photos_count', 0)}")
        if data.get("common_groups"):
            lines.append(f"👥 Общих групп: {data['common_groups']}")
        if data.get("is_contact"):
            lines.append("📞 В контактах")
        if data.get("is_mutual"):
            lines.append("🤝 Взаимный контакт")

    if data.get("_partial"):
        lines.append(
            "\n⚠️ <i>Данные ограничены — нет активного аккаунта в пуле.\n"
            "Добавьте аккаунт для получения полной статистики.</i>"
        )

    return "\n".join(lines)


def format_stats(data: dict) -> str:
    et = data.get("entity_type", "")
    if et not in ("channel", "supergroup"):
        return "📈 <b>Статистика</b>\n\n<i>Доступна только для каналов и групп.</i>"

    m = data.get("members", 0)
    av = data.get("avg_views", 0)
    af = data.get("avg_fwd", 0)
    ar = data.get("avg_react", 0)
    arl = data.get("avg_replies", 0)
    er = data.get("engagement_rate", 0)
    ppd = data.get("posts_per_day", 0)
    n = data.get("posts_analyzed", 0)

    # Reach rate: avg_views vs members
    reach = av / m * 100 if m else 0

    lines = ["📈 <b>Статистика канала</b>"]
    lines.append(f"\n👥 Подписчики: <b>{_num(m)}</b>")
    if data.get("online_count"):
        lines.append(f"🟢 Сейчас онлайн: <b>{_num(data['online_count'])}</b>")
    lines.append(f"👮 Администраторов: {data.get('admins_count', '—')}")
    lines.append(f"🤖 Ботов: {data.get('bot_count', '—')}")

    if n:
        lines.append(f"\n<i>Анализ {n} последних постов:</i>")
        lines.append(f"👁 Среднее просмотров: <b>{_num(av)}</b>")
        lines.append(f"↗️ Средний охват: <b>{_pct(av, m)}</b> от подписчиков")
        lines.append(f"🔁 Средних репостов: <b>{_num(af)}</b>")
        lines.append(f"❤️ Средних реакций: <b>{_num(ar)}</b>")
        if arl:
            lines.append(f"💬 Средних ответов: <b>{_num(arl)}</b>")
        lines.append(f"📊 Вовлечённость (ER): <b>{er:.2f}%</b>")
        lines.append(f"📅 Постов в день: <b>{ppd:.1f}</b>")
        lines.append(f"🏆 Макс. просмотров: <b>{_num(data.get('max_views', 0))}</b>")

        # ER benchmark
        if er >= 5:
            lines.append("🔥 Отличная вовлечённость (>5%)")
        elif er >= 2:
            lines.append("✅ Хорошая вовлечённость (2-5%)")
        elif er >= 0.5:
            lines.append("ℹ️ Средняя вовлечённость (0.5-2%)")
        else:
            lines.append("⚠️ Низкая вовлечённость (<0.5%)")

    return "\n".join(lines)


def format_content(data: dict) -> str:
    et = data.get("entity_type", "")
    if et not in ("channel", "supergroup"):
        return "📝 <b>Контент</b>\n\n<i>Доступна только для каналов и групп.</i>"

    lines = ["📝 <b>Контент и активность</b>"]

    # Media types
    mt = data.get("media_types", {})
    if mt:
        lines.append("\n<b>Типы контента:</b>")
        total = sum(mt.values())
        for mtype, cnt in mt.items():
            bar = _bar(cnt, total, 6)
            lines.append(f"  {mtype}: {bar} {_pct(cnt, total)}")

    # Top hashtags
    tags = data.get("top_hashtags", [])
    if tags:
        lines.append("\n<b>Топ хэштегов:</b>")
        for tag, cnt in tags[:5]:
            lines.append(f"  #{tag} — {cnt} раз")

    # Post length
    apl = data.get("avg_post_length", 0)
    if apl:
        lines.append(f"\n✏️ Средняя длина поста: <b>{apl} символов</b>")

    # Activity by hour
    hd = data.get("hour_dist", {})
    ph = data.get("peak_hour")
    if hd and ph is not None:
        lines.append(f"\n⏰ Пик активности: <b>{ph:02d}:00 UTC</b>")
        # Build mini chart for top 6 hours
        top_hours = sorted(hd.items(), key=lambda x: x[1], reverse=True)[:6]
        max_h = top_hours[0][1] if top_hours else 1
        lines.append("<b>Часы публикаций (UTC):</b>")
        for h, c in sorted(top_hours, key=lambda x: x[0]):
            bar = _bar(c, max_h, 5)
            lines.append(f"  {h:02d}:00 {bar} ({c})")

    # Top posts
    tp = data.get("top_posts", [])
    if tp:
        lines.append("\n🏆 <b>Топ постов по просмотрам:</b>")
        for i, p in enumerate(tp, 1):
            txt = html.escape(p.get("text", "") or "")
            v = _num(p["views"])
            r = _num(p["reactions"])
            url = p.get("url", "")
            if url:
                lines.append(f"  {i}. <a href='{url}'>{v} 👁 {r} ❤️</a> — {txt}")
            else:
                lines.append(f"  {i}. {v} 👁 {r} ❤️ — {txt}")

    return "\n".join(lines)


def format_network(data: dict) -> str:
    lines = ["🔗 <b>Сеть и связи</b>"]

    et = data.get("entity_type", "")
    if et not in ("channel", "supergroup"):
        lines.append("\n<i>Доступна только для каналов и групп.</i>")
        return "\n".join(lines)

    ln = data.get("linked_name")
    lid = data.get("linked_chat_id")
    if ln and lid:
        lt = "группа обсуждений" if et == "channel" else "связанный канал"
        lines.append(f"\n🔗 {lt.capitalize()}: <b>{html.escape(ln)}</b> (<code>{lid}</code>)")
    else:
        lines.append("\n<i>Нет связанного чата/канала.</i>")

    # Noforwards
    if data.get("noforwards"):
        lines.append("🚫 Пересылка сообщений <b>запрещена</b>")
    else:
        lines.append("✅ Пересылка сообщений разрешена")

    # Forward stats from content
    af = data.get("avg_fwd", 0)
    if af:
        lines.append(f"↗️ Среднее репостов: <b>{_num(af)}</b> на пост")
        vir = af / max(data.get("avg_views", 1), 1) * 100
        lines.append(f"📊 Virality rate: <b>{vir:.1f}%</b>")

    lines.append(
        "\n💡 <i>Глубокий анализ пересылок из других каналов "
        "доступен в модуле Конкурентов.</i>"
    )
    return "\n".join(lines)


def format_seo(data: dict) -> str:
    et = data.get("entity_type", "")
    if et not in ("channel", "supergroup"):
        return "🔍 <b>SEO-анализ</b>\n\n<i>Доступна только для каналов и групп.</i>"

    score = data.get("seo_score", 0)
    notes = data.get("seo_notes", [])

    bar_width = 10
    filled = round(bar_width * score / 100)
    bar = "█" * filled + "░" * (bar_width - filled)

    if score >= 80:
        grade = "🏆 Отлично"
    elif score >= 60:
        grade = "✅ Хорошо"
    elif score >= 40:
        grade = "⚠️ Средне"
    else:
        grade = "❌ Слабо"

    lines = [
        "🔍 <b>SEO-анализ</b>",
        f"\nОценка: <b>{score}/100</b>  {grade}",
        f"[{bar}]",
    ]

    title = data.get("title") or data.get("name") or ""
    desc = data.get("description", "")
    uname = data.get("username")

    lines.append(f"\n<b>Название:</b> «{html.escape(title)}» — {len(title)} симв.")
    lines.append(f"<b>Описание:</b> {len(desc)} симв.")
    lines.append(f"<b>Username:</b> {'@' + uname if uname else '❌ нет'}")
    lines.append(f"<b>Постов/день:</b> {data.get('posts_per_day', 0):.1f}")

    if notes:
        lines.append("\n<b>Рекомендации:</b>")
        for n in notes:
            lines.append(f"  {n}")
    else:
        lines.append("\n✅ SEO-показатели в норме!")

    return "\n".join(lines)


def format_admins(data: dict) -> str:
    et = data.get("entity_type", "")
    if et not in ("channel", "supergroup"):
        return "👮 <b>Администраторы</b>\n\n<i>Доступна только для каналов и групп.</i>"

    al = data.get("admin_list", [])
    lines = [f"👮 <b>Администраторы</b> ({data.get('admins_count', len(al))})"]

    for a in al:
        name = html.escape(a.get("name", "") or "")
        un = a.get("username")
        icon = "🤖" if a.get("is_bot") else ("✅" if a.get("verified") else "👤")
        un_str = f" @{un}" if un else ""
        lines.append(f"  {icon} {name}{un_str}  <code>{a['id']}</code>")

    bc = data.get("bot_count", 0)
    if bc:
        lines.append(f"\n🤖 Ботов в канале: <b>{bc}</b>")

    if not al:
        lines.append("\n<i>Список администраторов недоступен.</i>")

    return "\n".join(lines)


def format_export(data: dict) -> str:
    """Plain text full report for copying."""
    et = data.get("entity_type", "")
    title = data.get("title") or data.get("name") or ""
    uname = data.get("username", "")
    eid = data.get("entity_id", "")

    from services.registration_checker import format_date_ru, format_age
    ct = data.get("created_at")
    date_str = format_date_ru(ct) if ct else "—"
    age_str = format_age(ct) if ct else "—"

    lines = [
        f"=== Анализ: {title} ===",
        f"Тип: {et}",
        f"ID: {eid}",
        f"Username: @{uname}" if uname else "Username: нет",
        f"Создан: {date_str} ({age_str})",
    ]

    if et in ("channel", "supergroup"):
        lines += [
            f"Подписчики: {_num(data.get('members', 0))}",
            f"Вовлечённость: {data.get('engagement_rate', 0):.2f}%",
            f"Постов/день: {data.get('posts_per_day', 0):.1f}",
            f"Среднее просмотров: {_num(data.get('avg_views', 0))}",
            f"Среднее реакций: {_num(data.get('avg_react', 0))}",
            f"SEO: {data.get('seo_score', 0)}/100",
        ]
        desc = data.get("description", "")
        if desc:
            lines.append(f"Описание: {desc[:300]}")

    return "\n".join(lines)


# ── Page renderers ─────────────────────────────────────────────────────────────

PAGE_FORMATTERS = {
    0: format_overview,
    1: format_stats,
    2: format_content,
    3: format_network,
    4: format_seo,
    5: format_admins,
}

PAGE_TITLES = {
    0: "📊 Обзор",
    1: "📈 Статистика",
    2: "📝 Контент",
    3: "🔗 Сеть",
    4: "🔍 SEO",
    5: "👮 Администраторы",
}
