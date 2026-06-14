"""Topology Map — граф связей между активами системы.

Показывает связи между аккаунтами, каналами, группами и ботами:
- Какие каналы/группы привязаны к каждому аккаунту
- Какие боты управляют какими каналами
- Перекрёстные связи (cross-links между активами)

Формат: Telegram-native (текст + ASCII-диаграммы).
"""

from __future__ import annotations

import asyncio
import json
import asyncpg
import logging
from html import escape

from services.logger import log_exc_swallow

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AccCb, BotCb, TopoCb
from database import db
from services.account_manager import effective_account_status
from services import behavioral_engine

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 8


def _has_account_session(acc: dict) -> bool:
    return bool(acc.get("has_session")) or bool(
        acc.get("session_str") or acc.get("session_string")
    )


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
    has_data = bool(accounts or channels or bots)

    kb = InlineKeyboardBuilder()

    if not has_data:
        # Empty state: no assets at all
        kb.button(text="📱 Добавить аккаунт", callback_data=AccCb(action="menu"))
        kb.button(text="🤖 Добавить бота", callback_data=BotCb(action="list"))
        kb.button(text="🔄 Обновить граф", callback_data=TopoCb(action="menu"))
        kb.button(text="◀️ Назад", callback_data=AccCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "🗺️ <b>Topology Map — граф связей</b>\n\n"
            "📭 <b>Граф пуст — активов пока нет.</b>\n\n"
            "Чтобы карта отображала связи, добавьте:\n"
            "• <b>📱 Аккаунты</b> — Telegram-аккаунты в разделе «Аккаунты»\n"
            "• <b>📡 Каналы/группы</b> — импортируйте через «Каналы → Импорт из Telegram»\n"
            "• <b>🤖 Боты</b> — добавьте через «Мои боты»\n\n"
            "После добавления активов здесь появится интерактивная карта связей.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb.button(text="🗺️ Обзорная карта", callback_data=TopoCb(action="overview"))
    kb.button(text="📱 По аккаунтам", callback_data=TopoCb(action="acc_list", page=0))
    kb.button(text="📡 По каналам", callback_data=TopoCb(action="chan_list", page=0))
    kb.button(text="🔄 Обновить граф", callback_data=TopoCb(action="rebuild"))
    kb.button(text="📤 Экспорт графа", callback_data=TopoCb(action="export"))
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
            "name": a.get("first_name")
            or a.get("username")
            or a.get("phone")
            or f"acc#{a['id']}",
            "phone": a.get("phone", ""),
            "channels": [],
            "status": effective_account_status(
                a.get("acc_status"),
                has_session=_has_account_session(a),
                is_active=bool(a.get("is_active", True)),
            ),
        }
    for ch in channels:
        aid = ch.get("acc_id")
        if aid and aid in acc_map:
            acc_map[aid]["channels"].append(
                ch.get("title") or ch.get("username") or f"chan#{ch['channel_id']}"
            )

    # Build bot → channels map (bots manage channels they broadcast to)
    lines: list[str] = ["🗺️ <b>Обзорная карта связей</b>\n"]
    lines.append("─" * 28)

    # ASCII diagram: each account node with connected channels
    status_icons = {
        "active": "✅",
        "cooldown": "⏳",
        "spamblock": "⚠️",
        "banned": "❌",
        "deactivated": "💀",
    }
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
            lines.append(
                f"   • @{escape(str(b.get('username', '?')))} — {escape(bn[:25])}"
            )
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
        log_exc_swallow(
            log, "Ошибка сбора статистики перекрёстных переходов для топологии"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=TopoCb(action="overview"))
    kb.button(text="◀️ Топология", callback_data=TopoCb(action="menu"))
    kb.adjust(1)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>...текст обрезан</i>"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Account list for topology drill-down ───────────────────────────────────────


@router.callback_query(TopoCb.filter(F.action == "acc_list"))
async def cb_topo_acc_list(
    callback: CallbackQuery, callback_data: TopoCb, pool: asyncpg.Pool
) -> None:
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
    chunk = accounts[start : start + _PAGE_SIZE]

    status_icons = {
        "active": "✅",
        "cooldown": "⏳",
        "spamblock": "⚠️",
        "banned": "❌",
        "deactivated": "💀",
    }

    lines = ["📱 <b>Топология — по аккаунтам</b>\n"]
    for acc in chunk:
        si = status_icons.get(
            effective_account_status(
                acc.get("acc_status"),
                has_session=_has_account_session(acc),
                is_active=bool(acc.get("is_active", True)),
            ),
            "❓",
        )
        name = (
            acc.get("first_name")
            or acc.get("username")
            or acc.get("phone")
            or f"acc#{acc['id']}"
        )
        cnt = acc_chan_count.get(acc["id"], 0)
        lines.append(f"\n{si} <b>{escape(str(name)[:28])}</b>")
        lines.append(f"   📡 Каналов: <b>{cnt}</b>")

    kb = InlineKeyboardBuilder()
    for acc in chunk:
        name = (
            acc.get("first_name")
            or acc.get("username")
            or acc.get("phone")
            or f"acc#{acc['id']}"
        )
        kb.button(
            text=f"📱 {str(name)[:18]}",
            callback_data=TopoCb(action="acc_view", acc_id=acc["id"]),
        )
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️ Назад", callback_data=TopoCb(action="acc_list", page=page - 1)
        )
    nav.button(text=f"{page + 1}/{total_pages}", callback_data=TopoCb(action="noop"))
    if page < total_pages - 1:
        nav.button(
            text="▶️ Вперёд", callback_data=TopoCb(action="acc_list", page=page + 1)
        )
    nav.button(text="🗺️ Меню", callback_data=TopoCb(action="menu"))
    nav.adjust(3, 1)
    kb.attach(nav)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Single account view — connected channels ───────────────────────────────────


@router.callback_query(TopoCb.filter(F.action == "acc_view"))
async def cb_topo_acc_view(
    callback: CallbackQuery, callback_data: TopoCb, pool: asyncpg.Pool
) -> None:
    owner_id = callback.from_user.id
    acc_id = callback_data.acc_id

    acc = await db.get_tg_account(pool, acc_id, owner_id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

    channels = await db.get_managed_channels(pool, owner_id, acc_id)

    # Record cross-navigation: user navigated from account-list into this account.
    # Each channel attached to this account is an edge in the topology graph.
    for ch in channels:
        chan_id = ch.get("channel_id") or ch.get("id")
        if chan_id:
            asyncio.create_task(
                behavioral_engine.record_cross_nav(
                    pool,
                    owner_id=owner_id,
                    from_type="account",
                    from_id=acc_id,
                    to_type="channel",
                    to_id=chan_id,
                )
            )
    name = (
        acc.get("first_name")
        or acc.get("username")
        or acc.get("phone")
        or f"acc#{acc_id}"
    )
    phone = acc.get("phone", "")
    status = effective_account_status(
        acc.get("acc_status"),
        has_session=_has_account_session(acc),
        is_active=bool(acc.get("is_active", True)),
    )

    st_icons = {
        "active": "✅",
        "cooldown": "⏳",
        "spamblock": "⚠️",
        "banned": "❌",
        "deactivated": "💀",
    }
    si = st_icons.get(status, "❓")

    lines = [
        "📱 <b>Топология аккаунта</b>",
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
        groups = [
            c for c in channels if c.get("type") in ("megagroup", "supergroup", "group")
        ]
        if chans:
            lines.append("  ▸ <b>Каналы:</b>")
            for c in chans[:15]:
                title = (
                    c.get("title") or c.get("username") or f"ID:{c.get('channel_id')}"
                )
                uname = f" @{escape(c['username'])}" if c.get("username") else ""
                lines.append(f"    • {escape(str(title)[:40])}{uname}")
            if len(chans) > 15:
                lines.append(f"    <i>...ещё {len(chans) - 15}</i>")
        if groups:
            lines.append("  ▸ <b>Группы:</b>")
            for g in groups[:15]:
                title = (
                    g.get("title") or g.get("username") or f"ID:{g.get('channel_id')}"
                )
                uname = f" @{escape(g['username'])}" if g.get("username") else ""
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
        text,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Channel list for topology drill-down ───────────────────────────────────────


@router.callback_query(TopoCb.filter(F.action == "chan_list"))
async def cb_topo_chan_list(
    callback: CallbackQuery, callback_data: TopoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    page = callback_data.page

    channels = await db.get_managed_channels(pool, owner_id)
    accounts = await db.get_tg_accounts(pool, owner_id)
    acc_lookup = {
        a["id"]: (
            a.get("first_name")
            or a.get("username")
            or a.get("phone")
            or f"acc#{a['id']}"
        )
        for a in accounts
    }

    total_pages = max(1, (len(channels) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = channels[start : start + _PAGE_SIZE]

    lines = ["📡 <b>Топология — по каналам</b>\n"]
    for ch in chunk:
        title = ch.get("title") or ch.get("username") or f"ID:{ch.get('channel_id')}"
        uname = f"@{escape(ch['username'])}" if ch.get("username") else ""
        acc_name = acc_lookup.get(ch.get("acc_id"), "неизв.")
        ctype = {"megagroup": "👥", "supergroup": "👥", "group": "👥"}.get(
            ch.get("type", ""), "📡"
        )
        lines.append(f"{ctype} <b>{escape(str(title)[:30])}</b> {uname}")
        lines.append(f"   ↳ аккаунт: {escape(str(acc_name)[:25])}")

    kb = InlineKeyboardBuilder()
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️ Назад", callback_data=TopoCb(action="chan_list", page=page - 1)
        )
    nav.button(text=f"{page + 1}/{total_pages}", callback_data=TopoCb(action="noop"))
    if page < total_pages - 1:
        nav.button(
            text="▶️ Вперёд", callback_data=TopoCb(action="chan_list", page=page + 1)
        )
    nav.button(text="🗺️ Меню", callback_data=TopoCb(action="menu"))
    nav.adjust(3, 1)
    kb.attach(nav)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
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


@router.callback_query(TopoCb.filter(F.action == "noop"))
async def cb_topo_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ── Rebuild graph — compute account→channel edges ─────────────────────────────


@router.callback_query(TopoCb.filter(F.action == "rebuild"))
async def cb_topo_rebuild(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Compute topology edges: for every (account, channel) pair owned by this
    user, record a cross_nav behavioral event so the topology graph is populated.
    This is the real "Обновить граф" — it fills behavioral_events with cross_nav
    entries representing account→channel structural links.
    """
    await callback.answer("🔄 Пересчитываю граф...")
    owner_id = callback.from_user.id

    accounts = await db.get_tg_accounts(pool, owner_id)
    channels = await db.get_managed_channels(pool, owner_id)
    bots = await db.get_bots(pool, owner_id)

    # Build edges: account → channel
    edge_count = 0
    for ch in channels:
        acc_id = ch.get("acc_id")
        chan_id = ch.get("channel_id") or ch.get("id")
        if acc_id and chan_id:
            try:
                await behavioral_engine.record_cross_nav(
                    pool,
                    owner_id=owner_id,
                    from_type="account",
                    from_id=acc_id,
                    to_type="channel",
                    to_id=chan_id,
                )
                edge_count += 1
            except Exception:
                log_exc_swallow(log, "Ошибка записи ребра account→channel")

    # Build edges: bot → account (bots belong to owners; structural link)
    for b in bots:
        bot_id = b.get("bot_id")
        if bot_id:
            for acc in accounts:
                try:
                    await behavioral_engine.record_cross_nav(
                        pool,
                        owner_id=owner_id,
                        from_type="bot",
                        from_id=bot_id,
                        to_type="account",
                        to_id=acc["id"],
                    )
                    edge_count += 1
                except Exception:
                    log_exc_swallow(log, "Ошибка записи ребра bot→account")
                break  # one representative edge per bot is enough

    kb = InlineKeyboardBuilder()
    kb.button(text="🗺️ Обзорная карта", callback_data=TopoCb(action="overview"))
    kb.button(text="◀️ Меню", callback_data=TopoCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🔄 <b>Граф пересчитан</b>\n\n"
        f"📱 Аккаунтов: <b>{len(accounts)}</b>\n"
        f"📡 Каналов/групп: <b>{len(channels)}</b>\n"
        f"🤖 Ботов: <b>{len(bots)}</b>\n"
        f"🔗 Рёбер записано: <b>{edge_count}</b>\n\n"
        "Перекрёстные связи сохранены в behavioral_events (тип cross_nav).",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Export topology as structured data ────────────────────────────────────────


@router.callback_query(TopoCb.filter(F.action == "export"))
async def cb_topo_export(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Export topology graph as JSON-like text: nodes + edges."""
    await callback.answer("📤 Формирую экспорт...")
    owner_id = callback.from_user.id

    accounts = await db.get_tg_accounts(pool, owner_id)
    channels = await db.get_managed_channels(pool, owner_id)
    bots = await db.get_bots(pool, owner_id)

    # Nodes
    nodes: list[dict] = []
    for a in accounts:
        nodes.append({
            "type": "account",
            "id": a["id"],
            "name": a.get("first_name") or a.get("username") or a.get("phone") or f"acc#{a['id']}",
        })
    for ch in channels:
        nodes.append({
            "type": "channel",
            "id": ch.get("channel_id") or ch.get("id"),
            "name": ch.get("title") or ch.get("username") or f"chan#{ch.get('channel_id')}",
        })
    for b in bots:
        nodes.append({
            "type": "bot",
            "id": b.get("bot_id"),
            "name": b.get("first_name") or b.get("username") or f"bot#{b.get('bot_id')}",
        })

    # Edges from cross_nav behavioral events
    try:
        edge_rows = await pool.fetch(
            "SELECT meta FROM behavioral_events "
            "WHERE owner_id=$1 AND event_type='cross_nav' "
            "ORDER BY id DESC LIMIT 500",
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "Ошибка чтения рёбер из behavioral_events")
        edge_rows = []

    edges: list[dict] = []
    seen_edges: set[tuple] = set()
    for row in edge_rows:
        try:
            meta = json.loads(row["meta"])
            key = (meta.get("from_type"), meta.get("from_id"), meta.get("to_type"), meta.get("to_id"))
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({
                    "from": {"type": meta.get("from_type"), "id": meta.get("from_id")},
                    "to": {"type": meta.get("to_type"), "id": meta.get("to_id")},
                })
        except Exception as _exc:
            log.debug("topo_export: failed to parse edge meta: %s", _exc)

    export_data = {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "accounts": len(accounts),
            "channels": len(channels),
            "bots": len(bots),
            "edges": len(edges),
        },
    }

    # Format as readable text (Telegram has no file upload in callback)
    lines = [
        "📤 <b>Topology Export</b>\n",
        f"Узлов: <b>{len(nodes)}</b>  |  Рёбер: <b>{len(edges)}</b>\n",
        "<b>Узлы (nodes):</b>",
    ]
    for n in nodes[:20]:
        lines.append(f"  [{n['type']}:{n['id']}] {escape(str(n['name'])[:40])}")
    if len(nodes) > 20:
        lines.append(f"  <i>...ещё {len(nodes) - 20}</i>")

    lines.append("\n<b>Рёбра (edges):</b>")
    for e in edges[:15]:
        lines.append(
            f"  {e['from']['type']}:{e['from']['id']} → {e['to']['type']}:{e['to']['id']}"
        )
    if len(edges) > 15:
        lines.append(f"  <i>...ещё {len(edges) - 15}</i>")

    if not edges:
        lines.append(
            "  <i>Рёбра не найдены. Нажмите «🔄 Обновить граф» для пересчёта.</i>"
        )

    # Send as a separate message for copy-paste (full JSON in code block)
    json_preview = json.dumps(export_data["stats"], ensure_ascii=False)
    lines.append(f"\n<code>{escape(json_preview)}</code>")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить граф", callback_data=TopoCb(action="rebuild"))
    kb.button(text="◀️ Меню", callback_data=TopoCb(action="menu"))
    kb.adjust(1)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>...обрезано</i>"

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise
