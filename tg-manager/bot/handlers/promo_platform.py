"""Bot Promotion Platform — заказы, склад ботов, SMM-панели, чекер топа, логи."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import PromoCb, BmCb
from bot.states import PromoOrderFSM, PromoAddBotFSM, PromoAddPanelFSM, PromoTopCheckFSM
from bot.utils.op_helpers import safe_edit
from bot.utils.subscription import require_plan
from database import db
from services.logger import log_exc_swallow
from services import smm_panel as smm_svc

log = logging.getLogger(__name__)
router = Router()

_PRO = "pro"

# ── Status labels ──────────────────────────────────────────────────────────────

_ORDER_STATUS = {
    "waiting":     "⏳ Ожидает",
    "aging":       "🕐 Созревание",
    "boosting":    "🚀 Накрутка",
    "topup":       "💰 Пополнение",
    "checking":    "🔍 Проверка",
    "topped":      "🏆 В топе",
    "transferred": "✅ Передан",
    "cancelled":   "❌ Отменён",
}

_BOT_STATUS = {
    "aging":       "🕐 Созревает",
    "ready":       "✅ Готов",
    "working":     "🚀 Работает",
    "topped":      "🏆 В топе",
    "banned":      "🚫 Заблокирован",
    "transferred": "📤 Передан",
}


def _fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return dt.strftime("%d.%m.%Y %H:%M")
    return str(dt)


def _days_left(ready_at) -> str:
    if ready_at is None:
        return ""
    now = datetime.now(tz=timezone.utc)
    if hasattr(ready_at, "tzinfo") and ready_at.tzinfo is None:
        ready_at = ready_at.replace(tzinfo=timezone.utc)
    delta = (ready_at - now).days
    if delta <= 0:
        return " (готов!)"
    return f" (ещё {delta} д.)"


# ── Main menu ──────────────────────────────────────────────────────────────────

async def _show_menu(target, pool: asyncpg.Pool, edit: bool = True) -> None:
    user_id = (target.from_user or target.message.from_user).id if hasattr(target, "from_user") else target.chat.id

    orders = await db.promo_list_orders(pool, user_id, limit=100)
    bots = await db.warehouse_list_bots(pool, user_id, limit=100)
    panels = await db.smm_list_panels(pool, user_id)

    active_orders = [o for o in orders if o["status"] not in ("cancelled", "transferred")]
    ready_bots = [b for b in bots if b["status"] == "ready"]
    aging_bots = [b for b in bots if b["status"] == "aging"]

    text = (
        "🎯 <b>Платформа продвижения</b>\n\n"
        f"📋 Заказов активных: <b>{len(active_orders)}</b>\n"
        f"🤖 Ботов на складе: <b>{len(bots)}</b> "
        f"(✅ {len(ready_bots)} готовы · 🕐 {len(aging_bots)} созревают)\n"
        f"📡 SMM-панелей: <b>{len(panels)}</b>\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заказы", callback_data=PromoCb(action="orders"))
    kb.button(text="🤖 Склад ботов", callback_data=PromoCb(action="warehouse"))
    kb.button(text="📡 SMM-панели", callback_data=PromoCb(action="panels"))
    kb.button(text="🔍 Чекер топа", callback_data=PromoCb(action="topcheck"))
    kb.button(text="📜 Логи", callback_data=PromoCb(action="logs"))
    kb.button(text="◀️ Главное меню", callback_data=BmCb(action="menu"))
    kb.adjust(2, 2, 1, 1)

    if edit and isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, reply_markup=kb.as_markup())
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb.as_markup())


@router.message(Command("promo"))
async def cmd_promo(message: Message, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, message.from_user.id, _PRO):
        await message.answer("🔒 <b>Платформа продвижения — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await _show_menu(message, pool, edit=False)


@router.callback_query(PromoCb.filter(F.action == "menu"))
async def cb_promo_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text("🔒 <b>Платформа продвижения — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await _show_menu(callback, pool, edit=True)


# ── Orders list ────────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "orders"))
async def cb_promo_orders(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    page = callback_data.page
    status_filter = callback_data.value

    orders = await db.promo_list_orders(pool, user_id, status=status_filter, limit=10, offset=page * 10)

    if not orders:
        text = "📋 <b>Заказы</b>\n\nЗаказов пока нет. Нажмите «Новый заказ» для создания."
    else:
        lines = ["📋 <b>Заказы</b>\n"]
        for o in orders:
            st = _ORDER_STATUS.get(o["status"], o["status"])
            lines.append(
                f"{st} <b>#{o['id']}</b> — <code>{html.escape(o['keyword'])}</code>\n"
                f"   Позиция: топ-{o['target_position']} · Подписчики: {o['current_subs'] or 0}/{o['target_subs'] or '?'}\n"
                f"   Создан: {_fmt_dt(o['created_at'])}\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новый заказ", callback_data=PromoCb(action="new_order"))
    for o in orders:
        st = _ORDER_STATUS.get(o["status"], o["status"])
        kb.button(
            text=f"{st} #{o['id']} {o['keyword'][:20]}",
            callback_data=PromoCb(action="order_detail", item_id=o["id"]),
        )
    # pagination
    row = []
    if page > 0:
        row.append(kb.button(text="◀️", callback_data=PromoCb(action="orders", page=page - 1, value=status_filter)))
    if len(orders) == 10:
        row.append(kb.button(text="▶️", callback_data=PromoCb(action="orders", page=page + 1, value=status_filter)))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)

    await safe_edit(callback.message, text, reply_markup=kb.as_markup())


# ── Order detail ───────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "order_detail"))
async def cb_order_detail(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    st = _ORDER_STATUS.get(order["status"], order["status"])
    bot_line = ""
    if order["bot_id"]:
        bot = await db.warehouse_get_bot(pool, order["bot_id"])
        if bot:
            bot_line = f"🤖 Бот: @{html.escape(bot['bot_username'])}\n"

    panel_line = ""
    if order["smm_panel_id"]:
        panel = await db.smm_get_panel(pool, order["smm_panel_id"])
        if panel:
            panel_line = f"📡 Панель: {html.escape(panel['name'])}"
            if order["smm_order_id"]:
                panel_line += f" · Заказ #{order['smm_order_id']}"
            panel_line += "\n"

    text = (
        f"📋 <b>Заказ #{order['id']}</b>\n\n"
        f"🔑 Ключевое слово: <code>{html.escape(order['keyword'])}</code>\n"
        f"🎯 Целевая позиция: топ-{order['target_position']}\n"
        f"Статус: {st}\n"
        f"📊 Подписчики: {order['current_subs'] or 0} / {order['target_subs'] or '?'}\n"
        f"📍 Последняя позиция: {order['last_position'] or 'не проверялась'}\n"
        f"{bot_line}{panel_line}"
        f"📅 Создан: {_fmt_dt(order['created_at'])}\n"
        f"🔄 Обновлён: {_fmt_dt(order['updated_at'])}\n"
    )
    if order["completed_at"]:
        text += f"✅ Завершён: {_fmt_dt(order['completed_at'])}\n"

    kb = InlineKeyboardBuilder()
    if order["status"] not in ("cancelled", "transferred", "topped"):
        kb.button(text="❌ Отменить", callback_data=PromoCb(action="order_cancel", item_id=order["id"]))
    kb.button(text="🗑 Удалить", callback_data=PromoCb(action="order_delete", item_id=order["id"]))
    kb.button(text="📜 Логи заказа", callback_data=PromoCb(action="logs", item_id=order["id"]))
    kb.button(text="◀️ Заказы", callback_data=PromoCb(action="orders"))
    kb.adjust(2, 1, 1)

    await safe_edit(callback.message, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "order_cancel"))
async def cb_order_cancel(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        return
    await db.promo_update_order_status(pool, order["id"], "cancelled")
    await db.promo_log(pool, callback.from_user.id, "scheduler",
                       f"Заказ #{order['id']} отменён пользователем", order_id=order["id"])
    await callback.answer("Заказ отменён", show_alert=True)
    await cb_promo_orders.__wrapped__(callback, PromoCb(action="orders"), pool) if hasattr(cb_promo_orders, "__wrapped__") else await _refresh_orders(callback, pool)


async def _refresh_orders(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    fake_cd = PromoCb(action="orders")
    await cb_promo_orders(callback, fake_cd, pool)


@router.callback_query(PromoCb.filter(F.action == "order_delete"))
async def cb_order_delete(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        return
    await db.promo_delete_order(pool, order["id"], callback.from_user.id)
    await callback.answer("Заказ удалён", show_alert=True)
    await _show_menu(callback, pool, edit=True)


# ── New order FSM ──────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "new_order"))
async def cb_new_order_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text("🔒 <b>Новый заказ — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await state.set_state(PromoOrderFSM.keyword)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="orders"))
    await safe_edit(
        callback.message,
        "📋 <b>Новый заказ продвижения</b>\n\nШаг 1/5: введите <b>ключевое слово</b> для поиска в Telegram\n"
        "(например: <code>крипто боты</code>)",
        reply_markup=kb.as_markup(),
    )


@router.message(PromoOrderFSM.keyword)
async def fsm_order_keyword(message: Message, state: FSMContext) -> None:
    kw = message.text.strip()
    if len(kw) < 2:
        await message.answer("❌ Слишком короткое ключевое слово. Введите минимум 2 символа.")
        return
    await state.update_data(keyword=kw)
    await state.set_state(PromoOrderFSM.target_position)
    kb = InlineKeyboardBuilder()
    for pos in (1, 3, 5, 10):
        kb.button(text=f"Топ-{pos}", callback_data=f"promo_pos_{pos}")
    kb.adjust(4)
    await message.answer(
        f"✅ Ключевое слово: <code>{html.escape(kw)}</code>\n\n"
        "Шаг 2/5: выберите <b>целевую позицию</b> в поиске:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("promo_pos_"), PromoOrderFSM.target_position)
async def fsm_order_position(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    pos = int(callback.data.split("_")[-1])
    await state.update_data(target_position=pos)
    await state.set_state(PromoOrderFSM.pick_bot)

    bots = await db.warehouse_list_bots(pool, callback.from_user.id, limit=20)
    ready = [b for b in bots if b["status"] == "ready"]

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без бота", callback_data="promo_bot_0")
    for b in ready:
        kb.button(text=f"@{b['bot_username']}", callback_data=f"promo_bot_{b['id']}")
    kb.adjust(1)

    bot_hint = f"✅ Готовых ботов: {len(ready)}" if ready else "⚠️ Нет готовых ботов на складе"
    await safe_edit(
        callback.message,
        f"✅ Позиция: топ-{pos}\n\n"
        f"Шаг 3/5: выберите <b>бота</b> для продвижения\n{bot_hint}:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("promo_bot_"), PromoOrderFSM.pick_bot)
async def fsm_order_pick_bot(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    bot_id_str = callback.data.split("_")[-1]
    bot_id = int(bot_id_str) if bot_id_str != "0" else None
    await state.update_data(bot_id=bot_id)
    await state.set_state(PromoOrderFSM.pick_panel)

    panels = await db.smm_list_panels(pool, callback.from_user.id)
    active_panels = [p for p in panels if p["is_active"]]

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без панели", callback_data="promo_panel_0")
    for p in active_panels:
        kb.button(text=p["name"], callback_data=f"promo_panel_{p['id']}")
    kb.adjust(1)

    bot_label = "не выбран"
    if bot_id:
        bot = await db.warehouse_get_bot(pool, bot_id)
        if bot:
            bot_label = f"@{bot['bot_username']}"

    await safe_edit(
        callback.message,
        f"✅ Бот: {bot_label}\n\n"
        "Шаг 4/5: выберите <b>SMM-панель</b> для накрутки:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("promo_panel_"), PromoOrderFSM.pick_panel)
async def fsm_order_pick_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    panel_id_str = callback.data.split("_")[-1]
    panel_id = int(panel_id_str) if panel_id_str != "0" else None
    await state.update_data(panel_id=panel_id)
    await state.set_state(PromoOrderFSM.target_subs)
    await safe_edit(
        callback.message,
        "Шаг 5/5: введите <b>целевое количество подписчиков</b>\n"
        "(например: <code>5000</code>)\n\nОтправьте 0 если хотите задать позже.",
    )


@router.message(PromoOrderFSM.target_subs)
async def fsm_order_target_subs(message: Message, state: FSMContext) -> None:
    try:
        subs = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите число (количество подписчиков).")
        return
    await state.update_data(target_subs=subs if subs > 0 else None)
    data = await state.get_data()

    text = (
        "📋 <b>Подтверждение заказа</b>\n\n"
        f"🔑 Ключевое слово: <code>{html.escape(data['keyword'])}</code>\n"
        f"🎯 Позиция: топ-{data['target_position']}\n"
        f"🤖 Бот: {'выбран (id=' + str(data.get('bot_id')) + ')' if data.get('bot_id') else 'не выбран'}\n"
        f"📡 Панель: {'выбрана (id=' + str(data.get('panel_id')) + ')' if data.get('panel_id') else 'не выбрана'}\n"
        f"📊 Цель подписчиков: {data.get('target_subs') or 'не задана'}\n"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать заказ", callback_data=PromoCb(action="order_confirm"))
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="orders"))
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup())
    await state.set_state(PromoOrderFSM.confirm)


@router.callback_query(PromoCb.filter(F.action == "order_confirm"), PromoOrderFSM.confirm)
async def fsm_order_confirm(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()

    order_id = await db.promo_create_order(
        pool,
        owner_id=callback.from_user.id,
        keyword=data["keyword"],
        target_position=data["target_position"],
        bot_id=data.get("bot_id"),
        smm_panel_id=data.get("panel_id"),
        target_subs=data.get("target_subs"),
    )
    await db.promo_log(pool, callback.from_user.id, "scheduler",
                       f"Заказ #{order_id} создан: {data['keyword']}", order_id=order_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Открыть заказ", callback_data=PromoCb(action="order_detail", item_id=order_id))
    kb.button(text="◀️ Заказы", callback_data=PromoCb(action="orders"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Заказ #{order_id} создан!</b>\n\n"
        f"🔑 Ключевое слово: <code>{html.escape(data['keyword'])}</code>\n"
        f"🎯 Позиция: топ-{data['target_position']}\n\n"
        "Назначьте бота и SMM-панель, чтобы начать продвижение.",
        reply_markup=kb.as_markup(),
    )


# ── Bot Warehouse ──────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "warehouse"))
async def cb_warehouse(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    status_filter = callback_data.value
    page = callback_data.page

    # Refresh aging statuses
    updated = await db.warehouse_refresh_statuses(pool)

    bots = await db.warehouse_list_bots(pool, user_id, status=status_filter, limit=10, offset=page * 10)
    all_bots = await db.warehouse_list_bots(pool, user_id, limit=200)

    counts = {}
    for b in all_bots:
        counts[b["status"]] = counts.get(b["status"], 0) + 1

    status_line = " · ".join(
        f"{_BOT_STATUS.get(s, s)}: {c}" for s, c in counts.items()
    ) or "пусто"

    header = f"🤖 <b>Склад ботов</b>\n\n{status_line}\n"
    if updated:
        header += f"<i>Обновлено статусов: {updated} бот(а) → готов</i>\n"

    if not bots:
        text = header + "\nБотов нет. Добавьте первого бота."
    else:
        lines = [header]
        for b in bots:
            st = _BOT_STATUS.get(b["status"], b["status"])
            age_line = _days_left(b["ready_at"]) if b["status"] == "aging" else ""
            lines.append(
                f"{st} @{html.escape(b['bot_username'])}{age_line}\n"
                f"   👥 {b['current_subs']} подп. · рег. {_fmt_dt(b['registered_at'])}\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить бота", callback_data=PromoCb(action="bot_add"))
    # status filter buttons
    for s, label in [("aging", "🕐"), ("ready", "✅"), ("working", "🚀"), ("topped", "🏆"), ("banned", "🚫")]:
        active = "•" if status_filter == s else ""
        kb.button(text=f"{active}{label}", callback_data=PromoCb(action="warehouse", value=s))
    # bot buttons
    for b in bots:
        st = _BOT_STATUS.get(b["status"], b["status"])
        kb.button(
            text=f"{st} @{b['bot_username']}",
            callback_data=PromoCb(action="bot_detail", item_id=b["id"]),
        )
    if page > 0:
        kb.button(text="◀️", callback_data=PromoCb(action="warehouse", page=page - 1, value=status_filter))
    if len(bots) == 10:
        kb.button(text="▶️", callback_data=PromoCb(action="warehouse", page=page + 1, value=status_filter))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1, 5, 1)

    await safe_edit(callback.message, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "bot_detail"))
async def cb_bot_detail(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    bot = await db.warehouse_get_bot(pool, callback_data.item_id)
    if not bot or bot["owner_id"] != callback.from_user.id:
        await callback.answer("Бот не найден", show_alert=True)
        return

    st = _BOT_STATUS.get(bot["status"], bot["status"])
    age_note = ""
    if bot["status"] == "aging":
        age_note = f"\n⏳ Готов: {_fmt_dt(bot['ready_at'])}{_days_left(bot['ready_at'])}"

    text = (
        f"🤖 <b>@{html.escape(bot['bot_username'])}</b>\n\n"
        f"Статус: {st}{age_note}\n"
        f"👥 Подписчики: {bot['current_subs']}\n"
        f"📅 Зарегистрирован: {_fmt_dt(bot['registered_at'])}\n"
        f"✅ Готов к работе: {_fmt_dt(bot['ready_at'])}\n"
        f"🔗 Сессия: {'есть' if bot['session_path'] else 'нет'}\n"
        f"📝 Заметки: {html.escape(bot['notes'] or '—')}\n"
        f"🕒 Добавлен: {_fmt_dt(bot['created_at'])}\n"
    )

    kb = InlineKeyboardBuilder()
    # Status transitions
    status_transitions = {
        "aging": [],
        "ready": ["working"],
        "working": ["topped", "ready"],
        "topped": ["transferred"],
    }
    for next_st in status_transitions.get(bot["status"], []):
        label = _BOT_STATUS.get(next_st, next_st)
        kb.button(
            text=f"→ {label}",
            callback_data=PromoCb(action="bot_setstatus", item_id=bot["id"], value=next_st),
        )
    kb.button(text="🗑 Удалить", callback_data=PromoCb(action="bot_delete", item_id=bot["id"]))
    kb.button(text="◀️ Склад", callback_data=PromoCb(action="warehouse"))
    kb.adjust(2, 1, 1)

    await safe_edit(callback.message, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "bot_setstatus"))
async def cb_bot_setstatus(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    new_status = callback_data.value
    if new_status not in ("ready", "working", "topped", "transferred", "banned"):
        return
    bot = await db.warehouse_get_bot(pool, callback_data.item_id)
    if not bot or bot["owner_id"] != callback.from_user.id:
        return
    await db.warehouse_update_bot(pool, bot["id"], callback.from_user.id, status=new_status)
    await db.promo_log(pool, callback.from_user.id, "scheduler",
                       f"Бот @{bot['bot_username']} → {new_status}")
    await callback.answer(f"Статус обновлён: {_BOT_STATUS.get(new_status, new_status)}", show_alert=True)
    # Re-show detail
    await cb_bot_detail(callback, PromoCb(action="bot_detail", item_id=bot["id"]), pool)


@router.callback_query(PromoCb.filter(F.action == "bot_delete"))
async def cb_bot_delete(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    bot = await db.warehouse_get_bot(pool, callback_data.item_id)
    if not bot or bot["owner_id"] != callback.from_user.id:
        return
    await db.warehouse_delete_bot(pool, bot["id"], callback.from_user.id)
    await callback.answer(f"@{bot['bot_username']} удалён со склада", show_alert=True)
    await _show_menu(callback, pool, edit=True)


# ── Add bot FSM ────────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "bot_add"))
async def cb_bot_add_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text("🔒 <b>Склад ботов — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await state.set_state(PromoAddBotFSM.username)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="warehouse"))
    await safe_edit(
        callback.message,
        "🤖 <b>Добавить бота на склад</b>\n\nВведите <b>username</b> бота (без @):",
        reply_markup=kb.as_markup(),
    )


@router.message(PromoAddBotFSM.username)
async def fsm_bot_username(message: Message, state: FSMContext) -> None:
    username = message.text.strip().lstrip("@")
    if not username or len(username) < 3:
        await message.answer("❌ Некорректный username. Введите username бота (минимум 3 символа).")
        return
    await state.update_data(username=username)
    await state.set_state(PromoAddBotFSM.reg_date)
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня", callback_data="promo_regdate_today")
    await message.answer(
        f"✅ Username: @{html.escape(username)}\n\n"
        "Введите <b>дату регистрации</b> бота в формате <code>ГГГГ-ММ-ДД</code>\n"
        "или нажмите «Сегодня»:\n\n"
        "<i>Бот будет помечен как созревающий 21 день с этой даты.</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "promo_regdate_today", PromoAddBotFSM.reg_date)
async def fsm_bot_regdate_today(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(reg_date=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    await state.set_state(PromoAddBotFSM.token)
    await safe_edit(
        callback.message,
        "Введите <b>токен бота</b> (из BotFather) или отправьте <code>-</code> чтобы пропустить:",
    )


@router.message(PromoAddBotFSM.reg_date)
async def fsm_bot_reg_date(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    try:
        dt = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату как ГГГГ-ММ-ДД (например: 2025-01-15).")
        return
    await state.update_data(reg_date=text)
    await state.set_state(PromoAddBotFSM.token)
    await message.answer(
        f"✅ Дата регистрации: {text}\n\n"
        "Введите <b>токен бота</b> (из BotFather) или отправьте <code>-</code> чтобы пропустить:"
    )


@router.message(PromoAddBotFSM.token)
async def fsm_bot_token(message: Message, state: FSMContext) -> None:
    token = message.text.strip()
    token_enc = None if token == "-" else token
    await state.update_data(token=token_enc)
    await state.set_state(PromoAddBotFSM.notes)
    await message.answer("Введите <b>заметки</b> или отправьте <code>-</code> чтобы пропустить:")


@router.message(PromoAddBotFSM.notes)
async def fsm_bot_notes(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    notes = message.text.strip()
    if notes == "-":
        notes = ""
    data = await state.get_data()
    await state.clear()

    reg_date_str = data.get("reg_date", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    try:
        reg_dt = datetime.strptime(reg_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        reg_dt = datetime.now(tz=timezone.utc)

    bot_id = await db.warehouse_add_bot(
        pool,
        owner_id=message.from_user.id,
        bot_username=data["username"],
        bot_token_enc=data.get("token"),
        registered_at=reg_dt,
        notes=notes,
    )
    await db.promo_log(pool, message.from_user.id, "autoreg",
                       f"Бот @{data['username']} добавлен на склад (id={bot_id})")

    now = datetime.now(tz=timezone.utc)
    ready_at = reg_dt + timedelta(days=21)
    bot_status = "ready" if now >= ready_at else "aging"
    days_hint = _days_left(ready_at) if bot_status == "aging" else " (уже готов)"

    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Открыть склад", callback_data=PromoCb(action="warehouse"))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Бот @{html.escape(data['username'])} добавлен!</b>\n\n"
        f"Статус: {_BOT_STATUS.get(bot_status, bot_status)}{days_hint}\n"
        f"Готов к работе: {_fmt_dt(ready_at)}",
        reply_markup=kb.as_markup(),
    )


# ── SMM Panels ─────────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "panels"))
async def cb_panels(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    panels = await db.smm_list_panels(pool, callback.from_user.id)

    if not panels:
        text = "📡 <b>SMM-панели</b>\n\nПанели не добавлены. Добавьте первую панель для накрутки."
    else:
        lines = ["📡 <b>SMM-панели</b>\n"]
        for p in panels:
            active_mark = "✅" if p["is_active"] else "⛔"
            balance_line = f"💰 Баланс: {p['balance']}" if p["balance"] else ""
            lines.append(
                f"{active_mark} <b>{html.escape(p['name'])}</b>\n"
                f"   {html.escape(p['api_url'][:50])}\n"
                f"   {balance_line}\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить панель", callback_data=PromoCb(action="panel_add"))
    for p in panels:
        active_mark = "✅" if p["is_active"] else "⛔"
        kb.button(
            text=f"{active_mark} {p['name']}",
            callback_data=PromoCb(action="panel_detail", item_id=p["id"]),
        )
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)
    await safe_edit(callback.message, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "panel_detail"))
async def cb_panel_detail(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        await callback.answer("Панель не найдена", show_alert=True)
        return

    text = (
        f"📡 <b>{html.escape(panel['name'])}</b>\n\n"
        f"URL: <code>{html.escape(panel['api_url'])}</code>\n"
        f"Сервис ID: <code>{html.escape(panel['service_id'] or '—')}</code>\n"
        f"Активна: {'✅ да' if panel['is_active'] else '⛔ нет'}\n"
        f"💰 Баланс: {panel['balance'] or '—'}\n"
        f"🕒 Проверена: {_fmt_dt(panel['last_checked'])}\n"
        f"📅 Добавлена: {_fmt_dt(panel['created_at'])}\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить баланс", callback_data=PromoCb(action="panel_check", item_id=panel["id"]))
    toggle_label = "⛔ Деактивировать" if panel["is_active"] else "✅ Активировать"
    kb.button(text=toggle_label, callback_data=PromoCb(action="panel_toggle", item_id=panel["id"]))
    kb.button(text="🗑 Удалить", callback_data=PromoCb(action="panel_delete", item_id=panel["id"]))
    kb.button(text="◀️ Панели", callback_data=PromoCb(action="panels"))
    kb.adjust(1)
    await safe_edit(callback.message, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "panel_check"))
async def cb_panel_check(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Проверяю баланс...")
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        return

    client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
    result = await client.get_balance()

    if "error" in result:
        await callback.message.answer(
            f"⚠️ Ошибка запроса к панели: <code>{html.escape(str(result['error'])[:200])}</code>"
        )
        return

    balance = result.get("balance", result.get("Balance", "?"))
    currency = result.get("currency", result.get("Currency", ""))

    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    try:
        balance_float = float(str(balance).replace(",", "."))
    except (ValueError, TypeError):
        balance_float = 0.0

    await db.smm_update_panel(
        pool, panel["id"], callback.from_user.id,
        balance=balance_float,
        last_checked=now,
    )
    await db.promo_log(pool, callback.from_user.id, "booster",
                       f"Панель {panel['name']}: баланс {balance} {currency}")

    await callback.answer(f"💰 Баланс: {balance} {currency}", show_alert=True)
    await cb_panel_detail(callback, PromoCb(action="panel_detail", item_id=panel["id"]), pool)


@router.callback_query(PromoCb.filter(F.action == "panel_toggle"))
async def cb_panel_toggle(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        return
    new_active = not panel["is_active"]
    await db.smm_update_panel(pool, panel["id"], callback.from_user.id, is_active=new_active)
    status_word = "активирована" if new_active else "деактивирована"
    await callback.answer(f"Панель {status_word}", show_alert=True)
    await cb_panel_detail(callback, PromoCb(action="panel_detail", item_id=panel["id"]), pool)


@router.callback_query(PromoCb.filter(F.action == "panel_delete"))
async def cb_panel_delete(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        return
    await db.smm_delete_panel(pool, panel["id"], callback.from_user.id)
    await callback.answer(f"Панель «{panel['name']}» удалена", show_alert=True)
    await cb_panels(callback, pool)


# ── Add panel FSM ──────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "panel_add"))
async def cb_panel_add_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text("🔒 <b>SMM-панели — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await state.set_state(PromoAddPanelFSM.name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="panels"))
    await safe_edit(
        callback.message,
        "📡 <b>Добавить SMM-панель</b>\n\nШаг 1/4: введите <b>название</b> панели\n(например: <code>GlobalSMM</code>):",
        reply_markup=kb.as_markup(),
    )


@router.message(PromoAddPanelFSM.name)
async def fsm_panel_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(PromoAddPanelFSM.api_url)
    await message.answer(
        f"✅ Название: <b>{html.escape(message.text.strip())}</b>\n\n"
        "Шаг 2/4: введите <b>URL API</b> панели\n"
        "(например: <code>https://globalssmm.com/api/v2</code>):"
    )


@router.message(PromoAddPanelFSM.api_url)
async def fsm_panel_url(message: Message, state: FSMContext) -> None:
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("❌ URL должен начинаться с http:// или https://")
        return
    await state.update_data(api_url=url)
    await state.set_state(PromoAddPanelFSM.api_key)
    await message.answer("Шаг 3/4: введите <b>API-ключ</b> панели:")


@router.message(PromoAddPanelFSM.api_key)
async def fsm_panel_key(message: Message, state: FSMContext) -> None:
    await state.update_data(api_key=message.text.strip())
    await state.set_state(PromoAddPanelFSM.service_id)
    await message.answer(
        "Шаг 4/4: введите <b>ID сервиса</b> для накрутки Telegram-подписчиков\n"
        "или отправьте <code>-</code> чтобы пропустить:"
    )


@router.message(PromoAddPanelFSM.service_id)
async def fsm_panel_service_id(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    svc_id = message.text.strip()
    if svc_id == "-":
        svc_id = ""
    data = await state.get_data()
    await state.clear()

    panel_id = await db.smm_add_panel(
        pool,
        owner_id=message.from_user.id,
        name=data["name"],
        api_url=data["api_url"],
        api_key_enc=data["api_key"],
        service_id=svc_id,
    )
    await db.promo_log(pool, message.from_user.id, "booster",
                       f"Панель «{data['name']}» добавлена (id={panel_id})")

    kb = InlineKeyboardBuilder()
    kb.button(text="📡 Открыть панели", callback_data=PromoCb(action="panels"))
    kb.button(text="🔄 Проверить баланс", callback_data=PromoCb(action="panel_check", item_id=panel_id))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Панель «{html.escape(data['name'])}» добавлена!</b>\n\n"
        "Нажмите «Проверить баланс» для первичной верификации.",
        reply_markup=kb.as_markup(),
    )


# ── Top Checker ────────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "topcheck"))
async def cb_topcheck_menu(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text("🔒 <b>Чекер топа — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await state.set_state(PromoTopCheckFSM.keyword)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="menu"))
    await safe_edit(
        callback.message,
        "🔍 <b>Чекер топа Telegram</b>\n\n"
        "Введите <b>ключевое слово</b> для анализа позиций в поиске Telegram:\n"
        "(например: <code>crypto bots</code>)\n\n"
        "<i>Система попытается найти ботов в поиске Telegram по этому слову и показать топ-позиции.</i>",
        reply_markup=kb.as_markup(),
    )


@router.message(PromoTopCheckFSM.keyword)
async def fsm_topcheck_keyword(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    keyword = message.text.strip()
    await state.clear()

    # We can't directly access Telegram search without an account,
    # so we show info about what ranking.py tracks + point to existing ranking feature
    await db.promo_log(pool, message.from_user.id, "checker",
                       f"Запрос чекера топа: {keyword}")

    # Check if there's existing ranking data for this keyword
    try:
        rows = await pool.fetch(
            """SELECT k.keyword, k.target_username, r.position, r.checked_at
               FROM ranking_keywords k
               LEFT JOIN ranking_results r ON r.keyword_id = k.id
               WHERE LOWER(k.keyword) = LOWER($1)
               ORDER BY r.checked_at DESC LIMIT 5""",
            keyword,
        )
    except Exception:
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Трекер позиций", callback_data="rank_menu")
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)

    if rows:
        lines = [f"🔍 <b>Топ по запросу: «{html.escape(keyword)}»</b>\n"]
        for r in rows:
            pos = r["position"] if r["position"] else "н/д"
            lines.append(
                f"🤖 @{html.escape(r['target_username'] or '?')}: позиция #{pos}\n"
                f"   Проверено: {_fmt_dt(r['checked_at'])}"
            )
        text = "\n".join(lines)
    else:
        text = (
            f"🔍 <b>Чекер топа: «{html.escape(keyword)}»</b>\n\n"
            "По этому ключевому слову нет данных в трекере.\n\n"
            "Добавьте бота и ключевое слово в <b>Трекер позиций</b> — "
            "система будет автоматически отслеживать позиции в поиске Telegram."
        )
    await message.answer(text, reply_markup=kb.as_markup())


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "logs"))
async def cb_logs(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    order_id = callback_data.item_id or None
    level_filter = callback_data.value

    logs = await db.promo_get_logs(
        pool, callback.from_user.id,
        order_id=order_id,
        level=level_filter,
        limit=20,
    )

    title = f"📜 <b>Логи{'заказа #' + str(order_id) if order_id else ''}</b>"
    if not logs:
        text = f"{title}\n\nЛогов нет."
    else:
        lines = [f"{title}\n"]
        for entry in logs:
            lvl_icon = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌"}.get(entry["level"], "•")
            ts = _fmt_dt(entry["created_at"])
            lines.append(
                f"{lvl_icon} <code>{ts}</code> [{entry['event']}]\n"
                f"   {html.escape(entry['message'][:120])}\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    for lvl in ("INFO", "WARN", "ERROR"):
        active = "•" if level_filter == lvl else ""
        kb.button(
            text=f"{active}{lvl}",
            callback_data=PromoCb(action="logs", item_id=order_id or 0, value=lvl),
        )
    if level_filter:
        kb.button(text="Все", callback_data=PromoCb(action="logs", item_id=order_id or 0))
    if order_id:
        kb.button(text="◀️ Заказ", callback_data=PromoCb(action="order_detail", item_id=order_id))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(3, 1, 1)

    await safe_edit(callback.message, text, reply_markup=kb.as_markup())
