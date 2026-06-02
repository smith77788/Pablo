"""Network (multi-bot) management: clusters, routing weights, health, cross-bot ops + bulk."""

from __future__ import annotations
import asyncio
import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import NetworkCb, ClusterCb
from bot.keyboards import (
    network_ops_menu,
    network_clusters_menu,
    network_cluster_view,
    network_assign_bot_pick,
    network_routing_menu,
    network_clone_pick_source,
    network_clone_pick_dest,
    network_broadcast_confirm,
    subscription_locked_markup,
)
from bot.states import NetworkBroadcast, CloneSettings, SetRoutingWeight, AssignCluster
from bot.utils.subscription import require_plan, locked_text
from database import db
from services import broadcaster, bot_api

router = Router()

_ROLE_LABELS = {
    "entry": "🚪 Entry",
    "conversion": "💰 Conversion",
    "retention": "🔄 Retention",
    "general": "⚙️ General",
}


# ── Main menu ─────────────────────────────────────────────────────────────────

_LANG_HINT = (
    "Введите код языка (<code>ru</code>, <code>en</code>, <code>uk</code>, <code>de</code>…) "
    "или <code>-</code> чтобы сбросить до дефолтного."
)


async def _apply_all(
    pool: asyncpg.Pool, user_id: int, http: aiohttp.ClientSession, method, *args
) -> tuple[int, int, int]:
    bots = await db.get_bots(pool, user_id)
    if not bots:
        return 0, 0, 0
    results = await asyncio.gather(
        *(method(http, b["token"], *args) for b in bots),
        return_exceptions=True,
    )
    success = sum(1 for r in results if r is True)
    return success, len(results) - success, len(results)


def _result_text(ok: int, fail: int, total: int, action: str) -> str:
    return (
        f"📦 <b>Результат массового применения</b>\n\n"
        f"Действие: {action}\n"
        f"Всего ботов: {total}\n"
        f"✅ Успешно: {ok}\n"
        f"❌ Ошибок: {fail}"
    )


@router.callback_query(NetworkCb.filter(F.action == "menu"))
async def cb_net_menu(
    callback: CallbackQuery, callback_data: NetworkCb, pool: asyncpg.Pool
) -> None:

    await callback.answer()
    ov = await db.get_network_overview(pool, callback.from_user.id)
    swarm_pct = (
        round(ov["swarm_bots"] / ov["total_bots"] * 100) if ov["total_bots"] else 0
    )
    await callback.message.edit_text(
        f"🌐 <b>Сеть &amp; массовые операции</b>\n\n"
        f"🤖 Ботов: <b>{ov['total_bots']}</b> | "
        f"🧬 Swarm: <b>{ov['swarm_bots']}</b> ({swarm_pct}%)\n"
        f"🌐 Кластеров: <b>{ov['clusters']}</b>\n"
        f"👤 Уникальных юзеров: <b>{ov['unique_users']:,}</b>\n"
        f"📢 Сообщений отправлено: <b>{ov['total_sent']:,}</b>\n\n"
        "── <b>Управление сетью (PRO+)</b> ──\n"
        "📊 Аналитика — сводка по всей сети ботов\n"
        "🏆 Рейтинг — боты по размеру аудитории\n"
        "❤️ Здоровье — проверка доступности ботов\n"
        "👥 Пересечение — общие юзеры между ботами\n"
        "⚖️ Роутинг — распределение нагрузки (ENTERPRISE)\n"
        "🌐 Кластеры — группировка ботов (ENTERPRISE)\n\n"
        "── <b>Массовые правки</b> ──\n"
        "Изменить имя/описание/команды сразу для всех ботов\n\n"
        "💡 Кнопки с замком 🔒 откроются при повышении плана (/subscription)",
        parse_mode="HTML",
        reply_markup=network_ops_menu(),
    )


# ── Analytics (PRO) ───────────────────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "analytics"))
async def cb_net_analytics(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Аналитика сети", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    ov = await db.get_network_overview(pool, callback.from_user.id)
    bots = await db.get_bot_ranking(pool, callback.from_user.id)
    overlap = await db.get_bot_overlap_stats(pool, callback.from_user.id)
    mode = await db.get_system_mode(pool)

    top3 = bots[:3]
    top3_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, b in enumerate(top3):
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        top3_lines.append(
            f"  {medals[i]} {label} — {b['audience']:,} юз. / score {b['score']:.3f}"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="🏆 Полный рейтинг", callback_data=NetworkCb(action="ranking"))
    kb.button(text="👥 Пересечения", callback_data=NetworkCb(action="overlap"))
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    kb.adjust(2, 1)

    await callback.message.edit_text(
        f"📊 <b>Аналитика сети</b>\n\n"
        f"🤖 Всего ботов: <b>{ov['total_bots']}</b>\n"
        f"🧬 В Swarm: <b>{ov['swarm_bots']}</b> / {ov['total_bots']}\n"
        f"🌐 Кластеров: <b>{ov['clusters']}</b>\n"
        f"🌐 Режим: <b>{mode.upper()}</b>\n\n"
        f"<b>Аудитория:</b>\n"
        f"  Записей: {ov['total_users']:,}\n"
        f"  Уникальных: {ov['unique_users']:,}\n"
        f"  Пересечений: {overlap['multi_bot_users']:,} ({overlap['overlap_pct']}%)\n\n"
        f"<b>Рассылки отправлено:</b> {ov['total_sent']:,}\n"
        f"<b>Средний Score:</b> {ov['avg_score']:.3f}\n\n"
        + ("<b>Топ-3 по аудитории:</b>\n" + "\n".join(top3_lines) if top3 else ""),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Clusters (ENTERPRISE) ────────────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "clusters"))
async def cb_net_clusters(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Кластеры", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    clusters = await db.get_cluster_list(pool, callback.from_user.id)
    if not clusters:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text(
            "🌐 <b>Кластеры</b>\n\nКластеров пока нет.\n"
            "Назначьте боту кластер через «🧬 Swarm» → «Изменить кластер».",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await callback.message.edit_text(
            f"🌐 <b>Кластеры сети</b> — {len(clusters)} шт.\n\n"
            "Кластер объединяет ботов для совместного роутинга трафика.\n"
            "Нажмите кластер для управления:",
            parse_mode="HTML",
            reply_markup=network_clusters_menu(clusters),
        )
    await callback.answer()


@router.callback_query(ClusterCb.filter(F.action == "view"))
async def cb_cluster_view(
    callback: CallbackQuery, callback_data: ClusterCb, pool: asyncpg.Pool
) -> None:

    await callback.answer()
    cluster = callback_data.cluster or ""
    bots = await db.get_bots_in_cluster(pool, callback.from_user.id, cluster)
    total_aud = sum(b["audience_count"] for b in bots)
    swarm_on = sum(1 for b in bots if b["swarm_enabled"])
    mode = await db.get_system_mode(pool)

    lines = [
        f"🌐 <b>Кластер: {cluster}</b>\n",
        f"Ботов: {len(bots)} | В Swarm: {swarm_on} | Аудитория: {total_aud:,}",
        f"Режим: <b>{mode.upper()}</b>\n",
        "<b>Боты:</b>",
    ]
    for b in bots:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        swarm_icon = "🟢" if b["swarm_enabled"] else "⚫"
        role = _ROLE_LABELS.get(b.get("bot_role", "general"), "⚙️")
        lines.append(
            f"  {swarm_icon} {label} [{role}] — {b['audience_count']:,} юз. | score {b['score']:.3f}"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=network_cluster_view(cluster, bots),
    )


@router.callback_query(ClusterCb.filter(F.action == "bulk_swarm_on"))
async def cb_bulk_swarm_on(
    callback: CallbackQuery, callback_data: ClusterCb, pool: asyncpg.Pool
) -> None:

    cluster = callback_data.cluster or ""
    n = await db.bulk_set_swarm(pool, callback.from_user.id, cluster, True)
    await callback.answer(f"✅ Swarm включён для {n} ботов.", show_alert=True)
    bots = await db.get_bots_in_cluster(pool, callback.from_user.id, cluster)
    total_aud = sum(b["audience_count"] for b in bots)
    mode = await db.get_system_mode(pool)
    lines = [
        f"🌐 <b>Кластер: {cluster}</b>\n",
        f"Ботов: {len(bots)} | В Swarm: {n} | Аудитория: {total_aud:,}",
        f"Режим: <b>{mode.upper()}</b>\n",
        "<b>Боты:</b>",
    ]
    for b in bots:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        swarm_icon = "🟢" if b["swarm_enabled"] else "⚫"
        role = _ROLE_LABELS.get(b.get("bot_role", "general"), "⚙️")
        lines.append(f"  {swarm_icon} {label} [{role}] — {b['audience_count']:,} юз.")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=network_cluster_view(cluster, bots),
    )


@router.callback_query(ClusterCb.filter(F.action == "bulk_swarm_off"))
async def cb_bulk_swarm_off(
    callback: CallbackQuery, callback_data: ClusterCb, pool: asyncpg.Pool
) -> None:

    cluster = callback_data.cluster or ""
    n = await db.bulk_set_swarm(pool, callback.from_user.id, cluster, False)
    await callback.answer(f"⚫ Swarm отключён для {n} ботов.", show_alert=True)
    bots = await db.get_bots_in_cluster(pool, callback.from_user.id, cluster)
    mode = await db.get_system_mode(pool)
    lines = [
        f"🌐 <b>Кластер: {cluster}</b>\n",
        f"Режим: <b>{mode.upper()}</b>\n",
        "<b>Боты:</b>",
    ]
    for b in bots:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        lines.append(f"  ⚫ {label} — swarm off")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=network_cluster_view(cluster, bots),
    )


@router.callback_query(
    ClusterCb.filter(
        F.action.in_({"bulk_role_entry", "bulk_role_conversion", "bulk_role_retention"})
    )
)
async def cb_bulk_role(
    callback: CallbackQuery, callback_data: ClusterCb, pool: asyncpg.Pool
) -> None:

    cluster = callback_data.cluster or ""
    role_map = {
        "bulk_role_entry": "entry",
        "bulk_role_conversion": "conversion",
        "bulk_role_retention": "retention",
    }
    role = role_map[callback_data.action]
    n = await db.bulk_set_role(pool, callback.from_user.id, cluster, role)
    label = _ROLE_LABELS.get(role, role)
    await callback.answer(f"✅ Роль {label} назначена {n} ботам.", show_alert=True)
    bots = await db.get_bots_in_cluster(pool, callback.from_user.id, cluster)
    await callback.message.edit_text(
        f"🌐 <b>Кластер: {cluster}</b>\n\nРоль {label} применена к {n} ботам.",
        parse_mode="HTML",
        reply_markup=network_cluster_view(cluster, bots),
    )


@router.callback_query(ClusterCb.filter(F.action == "assign_start"))
async def cb_cluster_assign_start(
    callback: CallbackQuery,
    callback_data: ClusterCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        await callback.answer("Нет ботов.", show_alert=True)
        return
    await callback.answer()
    cluster = callback_data.cluster or ""
    await state.set_state(AssignCluster.waiting_name)
    await state.update_data(cluster=cluster)
    await callback.message.edit_text(
        f"🌐 <b>Назначить бота в кластер «{cluster}»</b>\n\nВыберите бота:",
        parse_mode="HTML",
        reply_markup=network_assign_bot_pick(cluster, list(bots)),
    )


@router.callback_query(ClusterCb.filter(F.action == "assign_confirm"))
async def cb_cluster_assign_confirm(
    callback: CallbackQuery,
    callback_data: ClusterCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    cluster = callback_data.cluster or ""
    await state.clear()
    await db.set_bot_cluster_name(
        pool, callback_data.bot_id, callback.from_user.id, cluster
    )
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = (
        f"@{row['username']}"
        if row and row["username"]
        else (row["first_name"] if row else str(callback_data.bot_id))
    )
    await callback.answer(f"✅ {label} → кластер «{cluster}»", show_alert=True)
    bots = await db.get_bots_in_cluster(pool, callback.from_user.id, cluster)
    mode = await db.get_system_mode(pool)
    lines = [
        f"🌐 <b>Кластер: {cluster}</b>\n",
        f"Режим: <b>{mode.upper()}</b>\n",
        "<b>Боты:</b>",
    ]
    for b in bots:
        lbl = f"@{b['username']}" if b["username"] else b["first_name"]
        swarm_icon = "🟢" if b["swarm_enabled"] else "⚫"
        lines.append(f"  {swarm_icon} {lbl} — {b['audience_count']:,} юз.")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=network_cluster_view(cluster, bots),
    )


# ── Bot Ranking (PRO) ─────────────────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "ranking"))
async def cb_net_ranking(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Рейтинг ботов", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    bots = await db.get_bot_ranking(pool, callback.from_user.id)
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 30
    lines = ["🏆 <b>Рейтинг ботов (по аудитории)</b>\n"]
    for i, b in enumerate(bots[:15]):
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        swarm = "🟢" if b["swarm_enabled"] else "⚫"
        role = _ROLE_LABELS.get(b.get("bot_role", "general"), "⚙️")
        cluster = b.get("cluster") or "default"
        lines.append(
            f"{medals[i]} <b>{label}</b> {swarm}\n"
            f"   👥 {b['audience']:,} | 📈 {b['score']:.3f} | {role} [{cluster}]"
        )
    if not bots:
        lines.append("Нет ботов.")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ── Routing Weights (PRO) ─────────────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "routing"))
async def cb_net_routing(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Веса роутинга", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    weights = await db.get_routing_weights_for_user(pool, callback.from_user.id)
    if not weights:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text(
            "⚖️ <b>Веса роутинга</b>\n\nНет ботов в Swarm.\n"
            "Включите Swarm для ботов через их меню → «🧬 Swarm».",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    total_weight = sum(float(w["weight"]) for w in weights)
    lines = [
        "⚖️ <b>Веса роутинга трафика</b>\n",
        "Вес определяет вероятность попасть именно в этот бот.\n",
    ]
    for w in weights:
        label = f"@{w['username']}" if w["username"] else w["first_name"]
        cluster = w.get("cluster") or "default"
        pct = round(float(w["weight"]) / total_weight * 100) if total_weight else 0
        lines.append(f"  • {label} [{cluster}] — вес {w['weight']:.1f} ({pct}%)")

    await callback.message.edit_text(
        "\n".join(lines) + "\n\n<i>Нажмите бота чтобы изменить его вес.</i>",
        parse_mode="HTML",
        reply_markup=network_routing_menu(list(weights)),
    )


@router.callback_query(NetworkCb.filter(F.action == "set_weight_pick"))
async def cb_set_weight_pick(
    callback: CallbackQuery,
    callback_data: NetworkCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await state.set_state(SetRoutingWeight.waiting_weight)
    await state.update_data(bot_id=callback_data.bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NetworkCb(action="routing"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚖️ <b>Вес роутинга для {label}</b>\n\n"
        "Введите вес — число от 0.1 до 10.0\n\n"
        "Примеры:\n"
        "  <code>1.0</code> — стандартный вес (по умолчанию)\n"
        "  <code>2.0</code> — в 2 раза больше трафика\n"
        "  <code>0.5</code> — в 2 раза меньше трафика\n"
        "  <code>0.0</code> — не получает трафик",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.message(SetRoutingWeight.waiting_weight, F.text)
async def msg_set_weight(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        weight = float(message.text.strip().replace(",", "."))
        if weight < 0 or weight > 10:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 0 до 10 (например: 1.5):")
        return
    data = await state.get_data()
    await state.clear()
    await db.set_routing_weight(pool, data["bot_id"], weight)
    row = await db.get_bot(pool, data["bot_id"], message.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else str(data["bot_id"])

    kb = InlineKeyboardBuilder()
    kb.button(text="⚖️ Все веса", callback_data=NetworkCb(action="routing"))
    kb.button(text="◀️ Сеть", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ Вес {label} установлен: <b>{weight:.1f}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NetworkCb.filter(F.action == "reset_weights"))
async def cb_reset_weights(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await db.reset_routing_weights(pool, callback.from_user.id)
    await callback.answer(
        "✅ Все веса сброшены до 1.0 (равное распределение).", show_alert=True
    )
    weights = await db.get_routing_weights_for_user(pool, callback.from_user.id)
    await callback.message.edit_text(
        "⚖️ <b>Веса роутинга</b>\n\nВсе веса сброшены — трафик распределяется равномерно.",
        parse_mode="HTML",
        reply_markup=network_routing_menu(list(weights)),
    )


# ── Health Check (PRO) ────────────────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "health"))
async def cb_net_health(
    callback: CallbackQuery, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Здоровье сети", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    bots = await db.get_network_health(pool, callback.from_user.id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text("❤️ Нет ботов.", reply_markup=kb.as_markup())
        return

    # Batch check all tokens concurrently
    results = await asyncio.gather(
        *(bot_api.get_me(http, b["token"]) for b in bots),
        return_exceptions=True,
    )

    lines = ["❤️ <b>Здоровье сети</b>\n"]
    ok_count = fail_count = 0
    for b, res in zip(bots, results):
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        if res and not isinstance(res, Exception):
            status = "✅"
            ok_count += 1
        else:
            status = "❌"
            fail_count += 1
        cluster = b.get("cluster") or "default"
        init = "⚡" if b["last_update_id"] > 1 else "💤"
        lines.append(
            f"{status} {label} [{cluster}] {init}\n"
            f"   👥 {b['audience']:,} | offset: {b['last_update_id']}"
        )

    lines.append(f"\n✅ Работают: {ok_count} | ❌ Ошибка: {fail_count}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=NetworkCb(action="health"))
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    kb.adjust(2)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Cross-bot Broadcast (ENTERPRISE) ─────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "broadcast"))
async def cb_net_broadcast(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Сетевая рассылка (legacy)", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    users = await db.get_unique_network_users(pool, callback.from_user.id)
    if not users:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text(
            "📢 <b>Сетевая рассылка</b>\n\nАудитория сети пуста.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    await state.set_state(NetworkBroadcast.waiting_message)
    await state.update_data(unique_count=len(users))
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NetworkCb(action="broadcast_cancel"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📢 <b>Сетевая рассылка</b>\n\n"
        f"Охват: <b>{len(users):,}</b> уникальных пользователей по всем ботам.\n\n"
        "Каждый пользователь получит сообщение от бота, с которым взаимодействовал последним.\n\n"
        "Напишите текст рассылки (HTML поддерживается):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(NetworkBroadcast.waiting_message, F.text)
async def msg_net_broadcast_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(text=message.text)
    await state.set_state(NetworkBroadcast.confirming)
    await message.answer(
        f"📢 <b>Предпросмотр сетевой рассылки:</b>\n\n{message.text}\n\n"
        f"Получателей: <b>{data['unique_count']:,}</b>\n\nЗапустить?",
        parse_mode="HTML",
        reply_markup=network_broadcast_confirm(),
    )


@router.callback_query(NetworkCb.filter(F.action == "broadcast_confirm"))
async def cb_net_broadcast_confirm(
    callback: CallbackQuery,
    callback_data: NetworkCb,
    state: FSMContext,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:

    data = await state.get_data()
    await state.clear()
    text = data.get("text", "")
    if not text:
        await callback.answer("Текст не найден.", show_alert=True)
        return
    await callback.answer()

    users = await db.get_unique_network_users(pool, callback.from_user.id)
    if not users:
        kb_empty = InlineKeyboardBuilder()
        kb_empty.button(text="◀️ К сети", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text(
            "📢 <b>Сетевая рассылка</b>\n\nАудитория сети пуста.",
            parse_mode="HTML",
            reply_markup=kb_empty.as_markup(),
        )
        return

    # Group by bot_id
    from collections import defaultdict

    by_bot: dict[int, list] = defaultdict(list)
    token_map: dict[int, str] = {}
    for u in users:
        by_bot[u["bot_id"]].append(u["user_id"])
        token_map[u["bot_id"]] = u["token"]

    total_users = sum(len(v) for v in by_bot.values())
    started_bots = 0
    for bot_id, user_ids in by_bot.items():
        token = token_map[bot_id]
        bc_id = await db.create_broadcast(
            pool, bot_id, text, len(user_ids), callback.from_user.id
        )
        broadcaster.start(pool, http, bc_id, token, bot_id, text, None, user_ids)
        started_bots += 1

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К сети", callback_data=NetworkCb(action="menu"))
    await callback.message.edit_text(
        f"🚀 <b>Сетевая рассылка запущена!</b>\n\n"
        f"Получателей: <b>{total_users:,}</b>\n"
        f"Задействовано ботов: <b>{started_bots}</b>\n\n"
        "Прогресс доступен в разделе «История рассылок» каждого бота.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NetworkCb.filter(F.action == "broadcast_cancel"))
async def cb_net_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К сети", callback_data=NetworkCb(action="menu"))
    await callback.message.edit_text(
        "❌ Рассылка отменена.", reply_markup=kb.as_markup()
    )


# ── Clone Settings (ENTERPRISE) ───────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "clone"))
async def cb_net_clone(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Клонирование настроек", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    bots = await db.get_bots(pool, callback.from_user.id)
    if len(bots) < 2:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
        await callback.message.edit_text(
            "🔄 <b>Клонирование настроек</b>\n\nНужно минимум 2 бота.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "🔄 <b>Клонирование настроек</b>\n\n"
        "Выберите <b>источник</b> — откуда копировать:\n"
        "(авто-ответы, цепочки, правила автоматизации)",
        parse_mode="HTML",
        reply_markup=network_clone_pick_source(list(bots)),
    )


@router.callback_query(NetworkCb.filter(F.action == "clone_pick_dest"))
async def cb_net_clone_pick_dest(
    callback: CallbackQuery,
    callback_data: NetworkCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    bots = await db.get_bots(pool, callback.from_user.id)
    src_row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not src_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    src_label = (
        f"@{src_row['username']}" if src_row["username"] else src_row["first_name"]
    )
    await state.set_state(CloneSettings.picking_dest)
    await state.update_data(src_id=callback_data.bot_id)
    await callback.message.edit_text(
        f"🔄 Источник: <b>{src_label}</b>\n\n"
        "Выберите <b>цель</b> — куда скопировать настройки:",
        parse_mode="HTML",
        reply_markup=network_clone_pick_dest(callback_data.bot_id, list(bots)),
    )


@router.callback_query(NetworkCb.filter(F.action == "clone_confirm"))
async def cb_net_clone_confirm(
    callback: CallbackQuery,
    callback_data: NetworkCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:

    data = await state.get_data()
    await state.clear()
    src_id = data.get("src_id")
    dst_id = callback_data.bot_id
    if not src_id or src_id == dst_id:
        await callback.answer("Ошибка: некорректные боты.", show_alert=True)
        return

    src_row = await db.get_bot(pool, src_id, callback.from_user.id)
    dst_row = await db.get_bot(pool, dst_id, callback.from_user.id)
    if not src_row or not dst_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    await callback.answer("⏳ Клонирую настройки…")
    counts = await db.clone_bot_settings(pool, src_id, dst_id)

    src_label = (
        f"@{src_row['username']}" if src_row["username"] else src_row["first_name"]
    )
    dst_label = (
        f"@{dst_row['username']}" if dst_row["username"] else dst_row["first_name"]
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Ещё клонирование", callback_data=NetworkCb(action="clone"))
    kb.button(text="◀️ К сети", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Клонирование завершено!</b>\n\n"
        f"Из: <b>{src_label}</b>\n"
        f"В: <b>{dst_label}</b>\n\n"
        f"📋 Авто-ответы: <b>{counts['auto_replies']}</b>\n"
        f"🔗 Цепочки: <b>{counts['funnels']}</b>\n"
        f"🤖 Правила автоматизации: <b>{counts['automation_rules']}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── User Overlap (PRO) ────────────────────────────────────────────────────────


@router.callback_query(NetworkCb.filter(F.action == "overlap"))
async def cb_net_overlap(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            locked_text("Пересечение аудиторий", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    stats = await db.get_bot_overlap_stats(pool, callback.from_user.id)
    bots = await db.get_bot_ranking(pool, callback.from_user.id)

    lines = [
        "👥 <b>Пересечение аудиторий</b>\n",
        f"Записей в БД: <b>{stats['total_entries']:,}</b>",
        f"Уникальных юзеров: <b>{stats['unique_users']:,}</b>",
        f"В нескольких ботах: <b>{stats['multi_bot_users']:,}</b> ({stats['overlap_pct']}%)\n",
        "<b>Аудитория по ботам:</b>",
    ]
    for b in bots[:10]:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        cluster = b.get("cluster") or "default"
        lines.append(f"  • {label} [{cluster}] — {b['audience']:,} юз.")

    if stats["multi_bot_users"] > 0:
        lines.append(
            f"\n💡 <i>{stats['multi_bot_users']:,} пользователей присутствуют в нескольких ботах.\n"
            f"Сетевая рассылка отправит им только 1 сообщение (дедупликация).</i>"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Сетевая рассылка", callback_data=NetworkCb(action="broadcast"))
    kb.button(text="◀️ Назад", callback_data=NetworkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
