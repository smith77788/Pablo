"""Bot Booster service layer: BotFather automation, SMM panels, search ranking."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from typing import Optional

import aiohttp
from telethon import TelegramClient
from telethon.network import ConnectionTcpObfuscated
from telethon.sessions import StringSession

from config import TG_API_ID, TG_API_HASH

log = logging.getLogger(__name__)

_BOTFATHER = "BotFather"
_CONNECT_TIMEOUT = 30
_CONV_TIMEOUT = 60


# ── Telethon client factory ───────────────────────────────────────────────────


def _parse_proxy(proxy_str: Optional[str]) -> Optional[tuple]:
    if not proxy_str:
        return None
    try:
        if "://" in proxy_str:
            scheme, rest = proxy_str.split("://", 1)
            if "@" in rest:
                auth, hostport = rest.rsplit("@", 1)
                user, pwd = auth.split(":", 1)
            else:
                hostport, user, pwd = rest, None, None
            host, port = hostport.rsplit(":", 1)
            stype = 5 if scheme.lower() in ("socks5",) else 4
            return (stype, host, int(port), True, user, pwd)
    except Exception:
        pass
    return None


async def _make_booster_client(session_str: str, proxy_str: Optional[str] = None) -> TelegramClient:
    proxy = _parse_proxy(proxy_str)
    client = TelegramClient(
        StringSession(session_str),
        TG_API_ID,
        TG_API_HASH,
        connection=ConnectionTcpObfuscated,
        proxy=proxy,
        device_model="iPhone 14 Pro",
        system_version="iOS 16.6",
        app_version="9.6.3",
        lang_code="en",
        system_lang_code="en-US",
        connection_retries=3,
        retry_delay=5,
    )
    return client


# ── Top Checker ───────────────────────────────────────────────────────────────


async def check_top(session_str: str, keyword: str, proxy: Optional[str] = None) -> list[dict]:
    """Search Telegram for bots by keyword. Returns ranked list with subscriber info."""
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import User

    results = []
    client = await _make_booster_client(session_str, proxy)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            return []
        search = await client(SearchRequest(q=keyword, limit=50))
        for entity in (search.users if hasattr(search, "users") else []):
            if isinstance(entity, User) and entity.bot:
                username = entity.username or ""
                results.append({
                    "username": username,
                    "first_name": entity.first_name or "",
                    "subscribers": 0,
                    "position": len(results) + 1,
                })
    except Exception as exc:
        log.warning("check_top error for keyword=%r: %s", keyword, exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return results


def calc_premiums_needed(position: int, our_subs: int, top1_subs: int) -> int:
    """Estimate premiums needed to reach top-1."""
    if position <= 1:
        return 0
    gap = max(0, top1_subs - our_subs) + 100
    return gap


# ── BotFather automation ──────────────────────────────────────────────────────


def _gen_bot_username(keyword: str) -> str:
    keyword_clean = re.sub(r"[^a-zA-Z0-9]", "", keyword)[:15]
    suffix = "".join(random.choices(string.digits, k=4))
    candidate = f"{keyword_clean}_{suffix}_bot"
    if not candidate.endswith("bot"):
        candidate = candidate + "bot"
    return candidate


def _extract_token(text: str) -> Optional[str]:
    m = re.search(r"\d{8,12}:[A-Za-z0-9_-]{35,}", text)
    return m.group(0) if m else None


async def register_bot(
    session_str: str,
    bot_name: str,
    bot_username: str,
    proxy: Optional[str] = None,
) -> Optional[str]:
    """Create a new bot via BotFather. Returns token or None."""
    client = await _make_booster_client(session_str, proxy)
    token = None
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            return None
        async with client.conversation(_BOTFATHER, timeout=_CONV_TIMEOUT) as conv:
            await conv.send_message("/newbot")
            r = await conv.get_response()
            if "name" not in r.text.lower() and "bot" not in r.text.lower():
                return None
            await conv.send_message(bot_name)
            r = await conv.get_response()
            if "username" not in r.text.lower():
                return None
            await conv.send_message(bot_username)
            r = await conv.get_response()
            token = _extract_token(r.text)
    except Exception as exc:
        log.warning("register_bot error username=%r: %s", bot_username, exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return token


async def parse_bots_from_botfather(
    session_str: str, proxy: Optional[str] = None
) -> list[dict]:
    """Extract all bots from BotFather /mybots. Returns list of {username, token}."""
    from telethon.tl.types import KeyboardButtonCallback

    client = await _make_booster_client(session_str, proxy)
    bots_found: list[dict] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            return []
        async with client.conversation(_BOTFATHER, timeout=_CONV_TIMEOUT) as conv:
            await conv.send_message("/mybots")
            msg = await conv.get_response()
            if not msg.buttons:
                return []
            for row in (msg.buttons or []):
                for btn in (row if isinstance(row, list) else [row]):
                    bot_username = btn.text.lstrip("@") if hasattr(btn, "text") else ""
                    if not bot_username:
                        continue
                    try:
                        await btn.click()
                        detail_msg = await conv.get_response()
                        token = None
                        if detail_msg.buttons:
                            for drow in detail_msg.buttons:
                                for dbtn in (drow if isinstance(drow, list) else [drow]):
                                    if "token" in (getattr(dbtn, "text", "") or "").lower() or "api" in (getattr(dbtn, "text", "") or "").lower():
                                        try:
                                            await dbtn.click()
                                            tok_msg = await conv.get_response()
                                            token = _extract_token(tok_msg.text or "")
                                        except Exception:
                                            pass
                                        break
                        bots_found.append({"username": bot_username, "token": token})
                        await conv.send_message("/mybots")
                        msg = await conv.get_response()
                    except Exception as e:
                        log.debug("parse_bots: error for %r: %s", bot_username, e)
                        continue
    except Exception as exc:
        log.warning("parse_bots_from_botfather error: %s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return bots_found


async def transfer_bot(
    session_str: str,
    bot_username: str,
    recipient_username: str,
    proxy: Optional[str] = None,
) -> bool:
    """Transfer bot ownership via BotFather. Returns True if transfer initiated."""
    client = await _make_booster_client(session_str, proxy)
    success = False
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            return False
        async with client.conversation(_BOTFATHER, timeout=_CONV_TIMEOUT) as conv:
            await conv.send_message("/mybots")
            msg = await conv.get_response()
            bot_btn = None
            for row in (msg.buttons or []):
                for btn in (row if isinstance(row, list) else [row]):
                    txt = getattr(btn, "text", "") or ""
                    if bot_username.lower().lstrip("@") in txt.lower():
                        bot_btn = btn
                        break
                if bot_btn:
                    break
            if not bot_btn:
                return False
            await bot_btn.click()
            detail = await conv.get_response()
            xfer_btn = None
            for row in (detail.buttons or []):
                for btn in (row if isinstance(row, list) else [row]):
                    txt = getattr(btn, "text", "") or ""
                    if "transfer" in txt.lower() or "передать" in txt.lower():
                        xfer_btn = btn
                        break
                if xfer_btn:
                    break
            if not xfer_btn:
                return False
            await xfer_btn.click()
            prompt = await conv.get_response()
            rcp = recipient_username.lstrip("@")
            await conv.send_message(f"@{rcp}")
            confirm = await conv.get_response()
            if "confirm" in confirm.text.lower() or "подтверди" in confirm.text.lower():
                if confirm.buttons:
                    for row in confirm.buttons:
                        for btn in (row if isinstance(row, list) else [row]):
                            txt = getattr(btn, "text", "") or ""
                            if "yes" in txt.lower() or "да" in txt.lower() or "confirm" in txt.lower():
                                await btn.click()
                                await conv.get_response()
                                success = True
                                break
                        if success:
                            break
            else:
                success = True
    except Exception as exc:
        log.warning("transfer_bot error bot=%r recipient=%r: %s", bot_username, recipient_username, exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return success


# ── SMM Panel API ─────────────────────────────────────────────────────────────


async def smm_get_balance(http: aiohttp.ClientSession, api_url: str, api_key: str) -> Optional[float]:
    try:
        async with http.post(api_url, data={"key": api_key, "action": "balance"}, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            return float(data.get("balance", 0))
    except Exception as exc:
        log.warning("smm_get_balance error url=%r: %s", api_url, exc)
        return None


async def smm_create_order(
    http: aiohttp.ClientSession,
    api_url: str,
    api_key: str,
    service_id: str,
    link: str,
    quantity: int,
    max_per_order: int = 5000,
) -> list[str]:
    """Create one or more SMM orders (smart split). Returns list of order IDs."""
    order_ids = []
    remaining = quantity
    while remaining > 0:
        chunk = min(remaining, max_per_order)
        try:
            async with http.post(api_url, data={
                "key": api_key,
                "action": "add",
                "service": service_id,
                "link": link,
                "quantity": chunk,
            }, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    break
                data = await r.json(content_type=None)
                oid = str(data.get("order", ""))
                if oid:
                    order_ids.append(oid)
                    remaining -= chunk
                else:
                    log.warning("smm_create_order: no order id in response: %s", data)
                    break
        except Exception as exc:
            log.warning("smm_create_order error: %s", exc)
            break
        await asyncio.sleep(1)
    return order_ids


async def smm_check_orders(
    http: aiohttp.ClientSession,
    api_url: str,
    api_key: str,
    order_ids: list[str],
) -> dict[str, dict]:
    """Check status of multiple orders. Returns {order_id: status_dict}."""
    if not order_ids:
        return {}
    ids_str = ",".join(order_ids)
    try:
        async with http.post(api_url, data={
            "key": api_key,
            "action": "status",
            "orders": ids_str,
        }, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return {}
            data = await r.json(content_type=None)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.warning("smm_check_orders error: %s", exc)
    return {}
