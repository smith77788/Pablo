"""Topology Map — граф связей между активами системы.

Показывает связи между аккаунтами, каналами, группами и ботами:
- Какие каналы/группы привязаны к каждому аккаунту
- Какие боты управляют какими каналами
- Перекрёстные связи (cross-links между активами)

Формат: Telegram-native (текст + ASCII-диаграммы).
"""
from __future__ import annotations

import asyncpg
import logging
from html import escape

from services.logger import log_exc_swallow

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AccCb, BotCb, ChanCb, TopoCb
from database import db

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 8


# ── Main menu ──────────────────────────────────────────────────────────────────

@router.callback_query(TopoCb.filter(F.action == "menu"))
async def cb_topo_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    # Gather stats
    accounts = await db.get_tg_accounts(pool, owner_id)
    channels = await db.get_managed_channels(pool, owner_id)
    bots = await db.get_bots(pool, owner_id)

    # Count distinct accs with channels
    accs_with_chans = len({c["acc_id"] for c in channels if c.get("acc_id")})

    kb = InlineKeyboardBuilder()
    kb.button(text="🗺️ Обзорная карта", callback_data=TopoCb(action="overview"))
    kb.button(text="📱 По аккаунтам", callback_data=TopoCb(action="acc_list", page=0))
    kb.button(text="📡 По каналам", callback_data=TopoCb(action="chan_list", page=0))
    kb.button(text="◀️ Назад", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🗺️ <b>Topology Map — граф связей</b>\n\n"
        f"📱 Аккаунтов: <b>{len(accounts)}</b>\n"
        f"📡 Каналов/групп: <b>{len(channels)}</b>\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"🔗 Аккаунтов с каналами: <b>{accs_with_chans}</b>\n\n"
        "Выберите представление ниже.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Overview map — ASCII таблица всех связей ──────────────────────────────────

@router.callback_query(TopoCb.filter(F.action == "overview"))
async def cb_topo_overview(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    accounts = await db.get_tg_accounts(pool, owner_id)
    channels = await db.get_managed_channels(pool, owner_id)
    bots = await db.get_bots(pool, owner_id)

    # Build account → channels map
    acc_map: dict[int, dict] = {}
    for a in accounts:
        acc_map[a["id"]] = {
            "name": a.get("first_name") or a.get("username") or a.get("phone") or f"acc#{a['id']}",
            "phone": a.get("phone", ""),
            "channels": [],
            "status": a.get("acc_status", "active"),
        }
    for ch in channels:
        aid = ch.get("acc_id")
        if aid and aid in acc_map:
            acc_map[aid]["channels"].append(ch.get("title") or ch.get("username") or f"chan#{ch['channel_id']}")

    # Build bot → channels map (bots manage channels they broadcast to)
    lines: list[str] = ["🗺️ <b>Обзорная карта связей</b>\n"]
    lines.append("─" * 28)

    # ASCII diagram: each account node with connected channels
    status_icons = {"active": "✅", "cooldown": "⏳", "spamblock": "⚠️", "banned": "❌", "deactivated": "💀"}
    for i, (aid, data) in enumerate(acc_map.items()):
        si = status_icons.get(data["status"], "❓")
        name = data["name"][:22]
        lines.append(f"\n{si} <b>{escape(name)}</b>")
        if data["phone"]:
            lines.append(f"   📞 <code>{escape(data['phone'])}</code>")

        chans = data["channels"]
        if not chans:
            lines.append("   └─ <i>нет каналов</i>")
        else:
            for j, cn in enumerate(chans[:8]):
                prefix = "   ├─" if j < len(chans[:8]) - 1 else "   └─"
                lines.append(f"{prefix} 📡 {escape(cn[:30])}")
            if len(chans) > 8:
                lines.append(f"   └─ <i>...ещё {len(chans) - 8}</i>")

    if bots:
        lines.append(f"\n🤖 <b>Боты ({len(bots)})</b>")
        for b in bots[:5]:
            bn = b.get("first_name") or b.get("username") or f"bot#{b.get('bot_id')}"
            lines.append(f"   • @{escape(str(b.get('username', '?')))} — {escape(bn[:25])}")
        if len(bots) > 5:
            lines.append(f"   <i>...ещё {len(bots) - 5}</i>")

    # Cross-link stats from behavioral events
    try:
        cross_row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM behavioral_events "
            "WHERE owner_id=$1 AND event_type='cross_nav'",
            owner_id,
        )
        cross_count = cross_row["cnt"] if cross_row else 0
        lines.append(f"\n🔗 Перекрёстных переходов: <b>{cross_count}</b>")
    except Exception:
        log_exc_swallow(log, "Ошибка сбора статистики перекрёстных переходов для топологии")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=TopoCb(action="overview"))
    kb.button(text="◀️ Топология", callback_data=TopoCb(action="menu"))
    kb.adjust(1)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>...текст обрезан</i>"

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Account list for topology drill-down ───────────────────────────────────────

@router.callback_query(TopoCb.filter(F.action == "acc_list"))
async def cb_topo_acc_list(callback: CallbackQuery, callback_data: TopoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    page = callback_data.page

    accounts = await db.get_tg_accounts(pool, owner_id)
    channels = await db.get_managed_channels(pool, owner_id)

    # Count channels per account
    acc_chan_count: dict[int, int] = {}
    for ch in channels:
        aid = ch.get("acc_id")
        if aid:
            acc_chan_count[aid] = acc_chan_count.get(aid, 0) + 1

    total_pages = max(1, (len(accounts) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = accounts[start:start + _PAGE_SIZE]

    status_icons = {"active": "✅", "cooldown": "⏳", "spamblock": "⚠️", "banned": "❌", "deactivated": "💀"}

    lines = ["📱 <b>Топология — по аккаунтам</b>\n"]
    for acc in chunk:
        si = status_icons.get(acc.get("acc_status", "active"), "❓")
        name = acc.get("first_name") or acc.get("username") or acc.get("phone") or f"acc#{acc['id']}"
        cnt = acc_chan_count.get(acc["id"], 0)
        lines.append(f"\n{si} <b>{escape(str(name)[:28])}</b>")
        lines.append(f"   📡 Каналов: <b>{cnt}</b>")

    kb = InlineKeyboardBuilder()
    for acc in chunk:
        name = acc.get("first_name") or acc.get("username") or acc.get("phone") or f"acc#{acc['id']}"
        kb.button(
            text=f"📱 {str(name)[:18]}",
            callback_data=TopoCb(action="acc_view", acc_id=acc["id"]),
        )
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ Назад", callback_data=TopoCb(action="acc_list", page=page - 1))
    nav.button(text=f"{page + 1}/{total_pages}", callback_data=TopoCb(action="noop"))
    if page < total_pages - 1:
        nav.button(text="▶️ Вперёд", callback_data=TopoCb(action="acc_list", page=page + 1))
    nav.button(text="🗺️ Меню", callback_data=TopoCb(action="menu"))
    nav.adjust(3, 1)
    kb.attach(nav)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Single account view — connected channels ───────────────────────────────────

@router.callback_query(TopoCb.filter(F.action == "acc_view"))
async def cb_topo_acc_view(callback: CallbackQuery, callback_data: TopoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    acc_id = callback_data.acc_id

    acc = await db.get_tg_account(pool, acc_id, owner_id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    channels = await db.get_managed_channels(pool, owner_id, acc_id)
    name = acc.get("first_name") or acc.get("username") or acc.get("phone") or f"acc#{acc_id}"
    phone = acc.get("phone", "")
    status = acc.get("acc_status", "active")

    st_icons = {"active": "✅", "cooldown": "⏳", "spamblock": "⚠️", "banned": "❌", "deactivated": "💀"}
    si = st_icons.get(status, "❓")

    lines = [
        f"📱 <b>Топология аккаунта</b>",
        f"{si} <b>{escape(str(name)[:35])}</b>",
    ]
    if phone:
        lines.append(f"📞 <code>{escape(phone)}</code>")
    lines.append("")

    if not channels:
        lines.append("📡 <i>Нет привязанных каналов/групп.</i>")
        lines.append("Используйте «📥 Импорт из Telegram» для сканирования.")
    else:
        lines.append(f"📡 <b>Каналы и группы ({len(channels)})</b>:")
        # group by type
        chans = [c for c in channels if c.get("type") in ("channel", None)]
        groups = [c for c in channels if c.get("type") in ("megagroup", "supergroup", "group")]
        if chans:
            lines.append("  ▸ <b>Каналы:</b>")
            for c in chans[:15]:
                title = c.get("title") or c.get("username") or f"ID:{c.get('channel_id')}"
                uname = f" @{c['username']}" if c.get("username") else ""
                lines.append(f"    • {escape(str(title)[:40])}{uname}")
            if len(chans) > 15:
                lines.append(f"    <i>...ещё {len(chans) - 15}</i>")
        if groups:
            lines.append("  ▸ <b>Группы:</b>")
            for g in groups[:15]:
                title = g.get("title") or g.get("username") or f"ID:{g.get('channel_id')}"
                uname = f" @{g['username']}" if g.get("username") else ""
                lines.append(f"    • {escape(str(title)[:40])}{uname}")
            if len(groups) > 15:
                lines.append(f"    <i>...ещё {len(groups) - 15}</i>")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад к списку", callback_data=TopoCb(action="acc_list", page=0))
    kb.button(text="🗺️ Меню", callback_data=TopoCb(action="menu"))
    kb.adjust(1)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>...текст обрезан</i>"

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Channel list for topology drill-down ───────────────────────────────────────

@router.callback_query(TopoCb.filter(F.action == "chan_list"))
async def cb_topo_chan_list(callback: CallbackQuery, callback_data: TopoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    page = callback_data.page

    channels = await db.get_managed_channels(pool, owner_id)
    accounts = await db.get_tg_accounts(pool, owner_id)
    acc_lookup = {a["id"]: (a.get("first_name") or a.get("username") or a.get("phone") or f"acc#{a['id']}")
                   for a in accounts}

    total_pages = max(1, (len(channels) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = channels[start:start + _PAGE_SIZE]

    lines = ["📡 <b>Топология — по каналам</b>\n"]
    for ch in chunk:
        title = ch.get("title") or ch.get("username") or f"ID:{ch.get('channel_id')}"
        uname = f"@{ch['username']}" if ch.get("username") else ""
        acc_name = acc_lookup.get(ch.get("acc_id"), "неизв.")
        ctype = {"megagroup": "👥", "supergroup": "👥", "group": "👥"}.get(ch.get("type", ""), "📡")
        lines.append(f"{ctype} <b>{escape(str(title)[:30])}</b> {uname}")
        lines.append(f"   ↳ аккаунт: {escape(str(acc_name)[:25])}")

    kb = InlineKeyboardBuilder()
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ Назад", callback_data=TopoCb(action="chan_list", page=page - 1))
    nav.button(text=f"{page + 1}/{total_pages}", callback_data=TopoCb(action="noop"))
    if page < total_pages - 1:
        nav.button(text="▶️ Вперёд", callback_data=TopoCb(action="chan_list", page=page + 1))
    nav.button(text="🗺️ Меню", callback_data=TopoCb(action="menu"))
    nav.adjust(3, 1)
    kb.attach(nav)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── /topology command ──────────────────────────────────────────────────────────

@router.message(Command("topology"))
async def cmd_topology(message: Message) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺️ Открыть Topology Map", callback_data=TopoCb(action="menu"))
    await message.answer(
        "🗺️ <b>Topology Map</b>\n\n"
        "Граф связей между аккаунтами, каналами, группами и ботами.\n"
        "Нажмите кнопку ниже чтобы открыть интерактивную карту.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
