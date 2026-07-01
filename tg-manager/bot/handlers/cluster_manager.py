"""Cluster Manager — manage bot clusters.

Entry point: ClustMCb(action="menu")
"""

from __future__ import annotations

import html as _html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ClustMCb, BotCb, NetBcCb, BmCb
from bot.keyboards import subscription_locked_markup
from services.logger import log_exc_swallow
from bot.states import CreateClusterFSM
from bot.utils.subscription import require_plan, locked_text

log = logging.getLogger(__name__)
router = Router()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать кластер", callback_data=ClustMCb(action="create"))
    kb.button(text="📋 Мои кластеры", callback_data=ClustMCb(action="list"))
    kb.button(text="📊 Статистика", callback_data=ClustMCb(action="stats"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="assets"))
    kb.adjust(2, 1, 1)
    return kb


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ClustMCb(action="menu"))
    return kb


def _cancel_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ClustMCb(action="menu"))
    return kb


# ── Menu ───────────────────────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "menu"))
async def cb_cluster_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Кластеры ботов", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="assets")),
        )
        return
    await callback.answer()
    await callback.message.edit_text(
        "🔗 <b>Кластеры ботов</b>\n\n"
        "Группируйте ботов по кластерам для совместного управления.",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── List clusters ──────────────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "list"))
async def cb_cluster_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """
            SELECT cluster, COUNT(*) AS bot_count
            FROM managed_bots
            WHERE added_by=$1 AND cluster IS NOT NULL AND is_active=TRUE
            GROUP BY cluster
            ORDER BY cluster
            """,
            user_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_list: DB fetch failed")
        rows = []

    lines = ["📋 <b>Мои кластеры</b>\n"]
    kb = InlineKeyboardBuilder()

    if not rows:
        lines.append(
            "Нет кластеров.\n\n"
            "Создайте кластер через <b>➕ Создать кластер</b> и назначьте ботов через раздел <b>Мои боты</b>."
        )
        kb.button(text="➕ Создать кластер", callback_data=ClustMCb(action="create"))
    else:
        for row in rows:
            cluster_name = row["cluster"]
            bot_count = row["bot_count"]
            lines.append(
                f"🔗 <b>{_html.escape(cluster_name)}</b> — {bot_count} бот(ов)"
            )
            kb.button(
                text=f"🔗 {cluster_name} ({bot_count})",
                callback_data=ClustMCb(action="view", cluster_name=cluster_name),
            )

    kb.button(text="◀️ Назад", callback_data=ClustMCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Stats ──────────────────────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "stats"))
async def cb_cluster_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """
            SELECT
                cluster,
                COUNT(*) AS bot_count,
                COALESCE(SUM(aud.cnt), 0) AS total_users
            FROM managed_bots m
            LEFT JOIN (
                SELECT bot_id, COUNT(*) AS cnt
                FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
            ) aud ON aud.bot_id = m.bot_id
            WHERE m.added_by=$1 AND m.cluster IS NOT NULL AND m.is_active=TRUE
            GROUP BY cluster
            ORDER BY total_users DESC
            """,
            user_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_stats: DB fetch failed")
        rows = []

    lines = ["📊 <b>Статистика кластеров</b>\n"]
    if not rows:
        lines.append(
            "Нет кластеров с ботами.\n\n"
            "Создайте кластер и назначьте ботов через раздел <b>Мои боты</b>."
        )
    else:
        for row in rows:
            lines.append(
                f"🔗 <b>{_html.escape(row['cluster'])}</b>\n"
                f"   Ботов: {row['bot_count']} | "
                f"Пользователей: {row['total_users']:,}"
            )

    kb = _back_kb()
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Create — step 1: name ─────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "create"))
async def cb_cluster_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Кластеры ботов", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="assets")),
        )
        return
    await callback.answer()
    await state.set_state(CreateClusterFSM.waiting_name)
    await callback.message.edit_text(
        "🔗 <b>Создать кластер</b>\n\n"
        "Введите название кластера (например: <code>ukraine</code>, <code>shop_bots</code>):",
        parse_mode="HTML",
        reply_markup=_cancel_kb().as_markup(),
    )


@router.message(CreateClusterFSM.waiting_name)
async def fsm_cluster_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer(
            "⚠️ Название должно быть от 1 до 64 символов.",
            reply_markup=_cancel_kb().as_markup(),
        )
        return

    await state.update_data(cluster_name=name)
    await state.set_state(CreateClusterFSM.waiting_description)

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ClustMCb(action="skip_desc"))
    kb.button(text="❌ Отмена", callback_data=ClustMCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "Введите описание кластера или нажмите <b>Пропустить</b>:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create — step 2: description ──────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "skip_desc"))
async def cb_skip_desc(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    data = await state.get_data()
    name = data.get("cluster_name", "")
    await _finish_cluster_create(callback.message, name, pool, callback.from_user.id)
    await state.clear()


@router.message(CreateClusterFSM.waiting_description)
async def fsm_cluster_desc(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    name = data.get("cluster_name", "")
    await _finish_cluster_create(message, name, pool, message.from_user.id)
    await state.clear()


async def _finish_cluster_create(
    message: Message, cluster_name: str, pool: asyncpg.Pool, owner_id: int
) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои кластеры", callback_data=ClustMCb(action="list"))
    kb.button(text="🏠 Меню кластеров", callback_data=ClustMCb(action="menu"))
    kb.adjust(1)

    # Persist the cluster to the database so it can be listed/used later.
    created = True
    try:
        result = await pool.execute(
            """INSERT INTO clusters (owner_id, name)
               VALUES ($1, $2)
               ON CONFLICT (owner_id, name) DO NOTHING""",
            owner_id,
            cluster_name,
        )
        # "INSERT 0 0" → кластер с таким именем уже существовал (устраняем ложный «создан»)
        created = str(result).split()[-1] != "0"
    except Exception:
        log_exc_swallow(log, "_finish_cluster_create: DB insert failed")

    head = (
        f"✅ Кластер <b>{cluster_name}</b> создан.\n\n"
        if created
        else f"ℹ️ Кластер <b>{cluster_name}</b> уже существует.\n\n"
    )
    await message.answer(
        head + "Назначьте ботов через раздел <b>Мои боты</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── View cluster bots ──────────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "view"))
async def cb_cluster_view(
    callback: CallbackQuery, callback_data: ClustMCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    cluster_name = callback_data.cluster_name or ""

    try:
        rows = await pool.fetch(
            """
            SELECT bot_id, username, first_name
            FROM managed_bots
            WHERE added_by=$1 AND cluster=$2 AND is_active=TRUE
            ORDER BY first_name
            """,
            user_id,
            cluster_name,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_detail: DB fetch failed")
        rows = []

    lines = [f"🔗 <b>Кластер: {_html.escape(cluster_name)}</b>\n"]
    kb = InlineKeyboardBuilder()

    if not rows:
        lines.append(
            "Нет ботов в этом кластере.\n\n"
            "Нажмите <b>➕ Добавить бота</b> чтобы прикрепить ботов к кластеру."
        )
    else:
        for bot_rec in rows:
            name = (
                bot_rec["username"] or bot_rec["first_name"] or f"id{bot_rec['bot_id']}"
            )
            lines.append(f"🤖 @{_html.escape(name)}")
            kb.button(
                text=f"➖ @{name}",
                callback_data=ClustMCb(action="remove_bot", cluster_name=cluster_name, bot_id=bot_rec["bot_id"]),
            )

    kb.button(
        text="➕ Добавить бота в кластер",
        callback_data=ClustMCb(action="add_bot_pick", cluster_name=cluster_name),
    )
    kb.button(
        text="📢 Рассылка по кластеру",
        callback_data=NetBcCb(action="cluster_broadcast", segment="cluster", cluster_name=cluster_name),
    )
    kb.button(
        text="🗑 Удалить кластер",
        callback_data=ClustMCb(action="delete_confirm", cluster_name=cluster_name),
    )
    kb.button(text="◀️ Назад", callback_data=ClustMCb(action="list"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Broadcast redirect ─────────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "broadcast"))
async def cb_cluster_broadcast(
    callback: CallbackQuery, callback_data: ClustMCb
) -> None:
    await callback.answer()
    cluster_name = callback_data.cluster_name or ""
    # Redirect to network broadcast with cluster segment
    await callback.message.edit_text(
        f"📢 Рассылка по кластеру <b>{_html.escape(cluster_name)}</b>\n\n"
        "Используйте раздел <b>Сетевая рассылка</b> и выберите нужный кластер.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder()
        .button(text="📢 Сетевая рассылка", callback_data=NetBcCb(action="menu"))
        .button(text="◀️ Назад", callback_data=ClustMCb(action="list"))
        .adjust(1)
        .as_markup(),
    )


# ── Delete cluster — confirm ───────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "delete_confirm"))
async def cb_cluster_delete_confirm(
    callback: CallbackQuery, callback_data: ClustMCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    cluster_name = callback_data.cluster_name or ""

    # Count bots assigned to this cluster
    try:
        bot_count = await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND cluster=$2 AND is_active=TRUE",
            user_id,
            cluster_name,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_delete_confirm: DB fetchval failed")
        bot_count = 0

    warning = ""
    if bot_count:
        warning = (
            f"\n\n⚠️ <b>Внимание:</b> в кластере {bot_count} бот(ов). "
            "После удаления они останутся активными, но потеряют привязку к кластеру."
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Да, удалить",
        callback_data=ClustMCb(action="delete", cluster_name=cluster_name),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=ClustMCb(action="view", cluster_name=cluster_name),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        f"🗑 <b>Удалить кластер «{_html.escape(cluster_name)}»?</b>{warning}\n\n"
        "Это действие необратимо.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Delete cluster — execute ───────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "delete"))
async def cb_cluster_delete(
    callback: CallbackQuery, callback_data: ClustMCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    cluster_name = callback_data.cluster_name or ""

    # Detach bots from cluster and remove cluster record
    try:
        await pool.execute(
            "UPDATE managed_bots SET cluster=NULL WHERE added_by=$1 AND cluster=$2",
            user_id,
            cluster_name,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_delete: detach bots failed")

    try:
        await pool.execute(
            "DELETE FROM clusters WHERE owner_id=$1 AND name=$2",
            user_id,
            cluster_name,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_delete: delete cluster row failed")

    await callback.message.edit_text(
        f"✅ Кластер <b>{_html.escape(cluster_name)}</b> удалён.",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── Add bot to cluster — pick ──────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "add_bot_pick"))
async def cb_cluster_add_bot_pick(
    callback: CallbackQuery, callback_data: ClustMCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    cluster_name = callback_data.cluster_name or ""

    # Bots NOT already in this cluster
    try:
        rows = await pool.fetch(
            """
            SELECT bot_id, username, first_name
            FROM managed_bots
            WHERE added_by=$1 AND is_active=TRUE
              AND (cluster IS NULL OR cluster != $2)
            ORDER BY first_name
            """,
            user_id,
            cluster_name,
        )
    except Exception:
        log_exc_swallow(log, "cb_cluster_add_bot_pick: DB fetch failed")
        rows = []

    kb = InlineKeyboardBuilder()
    if not rows:
        lines = [
            f"🔗 <b>Кластер: {_html.escape(cluster_name)}</b>\n",
            "Все активные боты уже в этом кластере.",
        ]
    else:
        lines = [
            f"🔗 <b>Добавить бота в кластер «{_html.escape(cluster_name)}»</b>\n",
            "Выберите бота:",
        ]
        for bot_rec in rows:
            name = (
                bot_rec["username"] or bot_rec["first_name"] or f"id{bot_rec['bot_id']}"
            )
            kb.button(
                text=f"🤖 @{name}",
                callback_data=ClustMCb(action="add_bot", cluster_name=cluster_name, bot_id=bot_rec["bot_id"]),
            )

    kb.button(text="◀️ Назад", callback_data=ClustMCb(action="view", cluster_name=cluster_name))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Add bot to cluster — execute ──────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "add_bot"))
async def cb_cluster_add_bot(
    callback: CallbackQuery, callback_data: ClustMCb, pool: asyncpg.Pool
) -> None:
    user_id = callback.from_user.id
    cluster_name = callback_data.cluster_name or ""
    bot_id = callback_data.bot_id

    try:
        await pool.execute(
            "UPDATE managed_bots SET cluster=$3 WHERE bot_id=$1 AND added_by=$2",
            bot_id,
            user_id,
            cluster_name,
        )
        await callback.answer(f"✅ Бот добавлен в кластер «{cluster_name}»", show_alert=False)
    except Exception:
        log_exc_swallow(log, "cb_cluster_add_bot: DB update failed")
        await callback.answer("❌ Ошибка при добавлении бота", show_alert=True)
        return

    # Refresh view
    from aiogram.types import CallbackQuery as CQ
    await cb_cluster_view(callback, callback_data, pool)


# ── Remove bot from cluster ────────────────────────────────────────────────────


@router.callback_query(ClustMCb.filter(F.action == "remove_bot"))
async def cb_cluster_remove_bot(
    callback: CallbackQuery, callback_data: ClustMCb, pool: asyncpg.Pool
) -> None:
    user_id = callback.from_user.id
    cluster_name = callback_data.cluster_name or ""
    bot_id = callback_data.bot_id

    try:
        await pool.execute(
            "UPDATE managed_bots SET cluster=NULL WHERE bot_id=$1 AND added_by=$2 AND cluster=$3",
            bot_id,
            user_id,
            cluster_name,
        )
        await callback.answer(f"✅ Бот удалён из кластера «{cluster_name}»", show_alert=False)
    except Exception:
        log_exc_swallow(log, "cb_cluster_remove_bot: DB update failed")
        await callback.answer("❌ Ошибка при удалении бота из кластера", show_alert=True)
        return

    # Refresh view
    await cb_cluster_view(callback, callback_data, pool)
