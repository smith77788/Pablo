"""Bot Promotion Platform — заказы, склад ботов, SMM-панели, чекер топа, логи.

Подсистемы:
  1. Конфигуратор заказов (FSM 5 шагов, статусы waiting→topped→transferred)
  2. Склад ботов (aging→ready автоматически за 21 день)
  3. SMM-панели (любой API v2, браузер сервисов, тест соединения)
  4. Чекер топа (через ranking tracker)
  5. Парсер ботов из BotFather
  6. Трансфер ботов через BotFather
  7. Загрузка .session файлов
  8. Логи (INFO/WARN/ERROR, фильтр по заказу)
  9. Запуск накрутки (прямой вызов SMM API)
 10. Уведомления (через promo_scheduler)
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone, timedelta

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Document, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, PromoCb, RankCb
from bot.states import (
    PromoAddBotFSM,
    PromoAddPanelFSM,
    PromoOrderFSM,
    PromoTopCheckFSM,
    PromoTransferFSM,
    PromoSessionUploadFSM,
)
from bot.utils.op_helpers import safe_edit
from bot.utils.subscription import require_plan
from database import db
from services import smm_panel as smm_svc
from services.logger import log_exc_swallow

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


# ── Cancel / back helpers ──────────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current:
        await state.clear()
        await message.answer("✅ Отменено. Откройте /promo для продолжения.")
    else:
        await message.answer("Нечего отменять. /promo — платформа продвижения.")


async def _cancel_fsm_and_back(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    await callback.answer()
    await _show_menu(callback, pool, edit=True)


# ── Main menu ──────────────────────────────────────────────────────────────────

async def _show_menu(target, pool: asyncpg.Pool, edit: bool = True) -> None:
    if isinstance(target, CallbackQuery):
        user_id = target.from_user.id
    else:
        user_id = target.from_user.id

    orders = await db.promo_list_orders(pool, user_id, limit=200)
    bots = await db.warehouse_list_bots(pool, user_id, limit=200)
    panels = await db.smm_list_panels(pool, user_id)

    active_orders = [o for o in orders if o["status"] not in ("cancelled", "transferred")]
    boosting_orders = [o for o in orders if o["status"] == "boosting"]
    ready_bots = [b for b in bots if b["status"] == "ready"]
    aging_bots = [b for b in bots if b["status"] == "aging"]
    active_panels = [p for p in panels if p["is_active"]]

    text = (
        "🎯 <b>Платформа продвижения ботов</b>\n\n"
        f"📋 Заказов активных: <b>{len(active_orders)}</b>"
        + (f" · 🚀 накручивается: <b>{len(boosting_orders)}</b>" if boosting_orders else "")
        + f"\n🤖 Ботов на складе: <b>{len(bots)}</b> "
        f"(✅ {len(ready_bots)} готовы · 🕐 {len(aging_bots)} созревают)\n"
        f"📡 SMM-панелей: <b>{len(active_panels)}</b> активных из {len(panels)}\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заказы", callback_data=PromoCb(action="orders"))
    kb.button(text="🤖 Склад ботов", callback_data=PromoCb(action="warehouse"))
    kb.button(text="📡 SMM-панели", callback_data=PromoCb(action="panels"))
    kb.button(text="🔍 Чекер топа", callback_data=PromoCb(action="topcheck"))
    kb.button(text="📁 Загрузить сессию", callback_data=PromoCb(action="session_upload"))
    kb.button(text="📜 Логи", callback_data=PromoCb(action="logs"))
    kb.button(text="◀️ Главное меню", callback_data=BmCb(action="menu"))
    kb.adjust(2, 2, 2, 1)

    if edit and isinstance(target, CallbackQuery):
        await safe_edit(target, text, reply_markup=kb.as_markup())
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
async def cb_promo_menu(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Платформа продвижения — 💎 ПОДПИСКА</b>\n\nОформите: /subscription"
        )
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
                f"   Топ-{o['target_position']} · "
                f"{o['current_subs'] or 0}/{o['target_subs'] or '?'} подп.\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новый заказ", callback_data=PromoCb(action="new_order"))
    # Status filter chips
    for s, label in [("boosting", "🚀"), ("waiting", "⏳"), ("topped", "🏆"), ("cancelled", "❌")]:
        mark = "•" if status_filter == s else ""
        kb.button(text=f"{mark}{label}", callback_data=PromoCb(action="orders", value=s))
    if status_filter:
        kb.button(text="Все", callback_data=PromoCb(action="orders"))
    for o in orders:
        st = _ORDER_STATUS.get(o["status"], o["status"])
        kb.button(
            text=f"{st} #{o['id']} {o['keyword'][:18]}",
            callback_data=PromoCb(action="order_detail", item_id=o["id"]),
        )
    nav: list[PromoCb] = []
    if page > 0:
        nav.append(PromoCb(action="orders", page=page - 1, value=status_filter))
    if len(orders) == 10:
        nav.append(PromoCb(action="orders", page=page + 1, value=status_filter))
    for cd in nav:
        kb.button(text="◀️" if cd.page < page else "▶️", callback_data=cd)
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1, 4, 1, 1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


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
        f"📍 Последняя позиция: "
        f"{'#' + str(order['last_position']) if order['last_position'] else 'не проверялась'}\n"
        f"{bot_line}{panel_line}"
        f"📅 Создан: {_fmt_dt(order['created_at'])}\n"
        f"🔄 Обновлён: {_fmt_dt(order['updated_at'])}\n"
    )
    if order["completed_at"]:
        text += f"✅ Завершён: {_fmt_dt(order['completed_at'])}\n"

    kb = InlineKeyboardBuilder()
    status = order["status"]

    # "Запустить накрутку" — если есть бот+панель+target_subs и статус waiting/checking
    can_boost = (
        status in ("waiting", "checking")
        and order["bot_id"]
        and order["smm_panel_id"]
        and order["target_subs"]
    )
    if can_boost:
        kb.button(text="🚀 Запустить накрутку", callback_data=PromoCb(action="order_boost", item_id=order["id"]))

    # "Обновить статус" — если уже идёт накрутка
    if status == "boosting" and order["smm_order_id"]:
        kb.button(text="🔄 Обновить статус", callback_data=PromoCb(action="order_check_smm", item_id=order["id"]))

    # "Отметить как в топе" — для checking/boosting
    if status in ("checking", "boosting"):
        kb.button(text="🏆 Отметить в топе", callback_data=PromoCb(action="order_mark_topped", item_id=order["id"]))

    if status not in ("cancelled", "transferred", "topped"):
        kb.button(text="❌ Отменить", callback_data=PromoCb(action="order_cancel", item_id=order["id"]))
    kb.button(text="🗑 Удалить", callback_data=PromoCb(action="order_delete", item_id=order["id"]))
    kb.button(text="📜 Логи заказа", callback_data=PromoCb(action="logs", item_id=order["id"]))
    kb.button(text="◀️ Заказы", callback_data=PromoCb(action="orders"))
    kb.adjust(1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "order_cancel"))
async def cb_order_cancel(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await db.promo_update_order_status(pool, order["id"], "cancelled")
    await db.promo_log(
        pool, callback.from_user.id, "scheduler",
        f"Заказ #{order['id']} отменён пользователем", order_id=order["id"]
    )
    await callback.answer("Заказ отменён ✓", show_alert=True)
    await cb_promo_orders(callback, PromoCb(action="orders"), pool)


@router.callback_query(PromoCb.filter(F.action == "order_delete"))
async def cb_order_delete(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await db.promo_delete_order(pool, order["id"], callback.from_user.id)
    await callback.answer("Заказ удалён ✓", show_alert=True)
    await cb_promo_orders(callback, PromoCb(action="orders"), pool)


@router.callback_query(PromoCb.filter(F.action == "order_mark_topped"))
async def cb_order_mark_topped(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    now = datetime.now(tz=timezone.utc)
    await db.promo_update_order_status(pool, order["id"], "topped", completed_at=now)
    await db.promo_log(
        pool, callback.from_user.id, "checker",
        f"Заказ #{order['id']} помечен как «В топе»", order_id=order["id"]
    )
    # Update bot status too if linked
    if order["bot_id"]:
        await db.warehouse_update_bot(pool, order["bot_id"], callback.from_user.id, status="topped")
    await callback.answer("🏆 Помечено как «В топе»", show_alert=True)
    await cb_order_detail(callback, PromoCb(action="order_detail", item_id=order["id"]), pool)


# ── Boost order via SMM panel ──────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "order_boost"))
async def cb_order_boost(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Отправляю заказ в панель...")
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    panel = await db.smm_get_panel(pool, order["smm_panel_id"])
    if not panel:
        await callback.message.answer("⚠️ SMM-панель не найдена. Проверьте настройки заказа.")
        return

    bot_rec = await db.warehouse_get_bot(pool, order["bot_id"])
    if not bot_rec:
        await callback.message.answer("⚠️ Бот не найден в складе.")
        return

    link = f"https://t.me/{bot_rec['bot_username']}"
    client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
    result = await client.add_order(
        service_id=panel["service_id"] or "",
        link=link,
        quantity=int(order["target_subs"] or 100),
    )

    if result.get("error") or "order" not in result:
        err_msg = result.get("error", str(result)[:200])
        await db.promo_log(
            pool, callback.from_user.id, "booster",
            f"Ошибка запуска накрутки заказ #{order['id']}: {err_msg}",
            level="ERROR", order_id=order["id"],
            meta={"panel": panel["name"], "link": link},
        )
        await callback.message.answer(
            f"❌ <b>Ошибка при отправке заказа в панель</b>\n\n"
            f"Панель: {html.escape(panel['name'])}\n"
            f"Ответ: <code>{html.escape(str(err_msg)[:300])}</code>\n\n"
            f"Проверьте:\n"
            f"• ID сервиса в настройках панели\n"
            f"• Баланс панели\n"
            f"• Корректность API ключа"
        )
        return

    smm_order_id = str(result["order"])
    await db.promo_update_order_status(
        pool, order["id"], "boosting",
        smm_order_id=smm_order_id,
        smm_panel_id=panel["id"],
    )
    await db.warehouse_update_bot(pool, bot_rec["id"], callback.from_user.id, status="working")
    await db.promo_log(
        pool, callback.from_user.id, "booster",
        f"Накрутка запущена: заказ #{order['id']} → панель #{smm_order_id}",
        order_id=order["id"],
        meta={"panel": panel["name"], "smm_order_id": smm_order_id, "link": link},
    )
    await callback.message.edit_text(
        f"✅ <b>Накрутка запущена!</b>\n\n"
        f"Заказ #{order['id']}: <code>{html.escape(order['keyword'])}</code>\n"
        f"Панель: {html.escape(panel['name'])} · SMM-заказ: #{smm_order_id}\n"
        f"Ссылка: {html.escape(link)}\n"
        f"Подписчиков: {order['target_subs']}\n\n"
        f"Статус будет проверяться каждые 15 минут автоматически.\n"
        f"Или используйте кнопку «Обновить статус» вручную.",
        reply_markup=InlineKeyboardBuilder().button(
            text="◀️ К заказу", callback_data=PromoCb(action="order_detail", item_id=order["id"])
        ).as_markup(),
    )


@router.callback_query(PromoCb.filter(F.action == "order_check_smm"))
async def cb_order_check_smm(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Проверяю статус в панели...")
    order = await db.promo_get_order(pool, callback_data.item_id)
    if not order or order["owner_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    panel = await db.smm_get_panel(pool, order["smm_panel_id"])
    if not panel or not order["smm_order_id"]:
        await callback.answer("Нет данных для проверки", show_alert=True)
        return

    client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
    result = await client.get_order_status(order["smm_order_id"])

    if result.get("error"):
        await callback.message.answer(
            f"⚠️ Ошибка при проверке статуса:\n<code>{html.escape(str(result['error'])[:200])}</code>"
        )
        return

    raw_status = result.get("status", "?")
    remains = result.get("remains", result.get("remain", "?"))
    charge = result.get("charge", "?")
    start_count = result.get("start_count", "?")
    normalized = smm_svc.normalize_status(raw_status)

    await db.promo_log(
        pool, callback.from_user.id, "booster",
        f"Ручная проверка заказа #{order['id']}: {normalized}",
        order_id=order["id"], meta={"raw": dict(result)},
    )

    await callback.message.answer(
        f"🔄 <b>Статус заказа #{order['smm_order_id']} на панели</b>\n\n"
        f"Статус: <b>{html.escape(normalized)}</b>\n"
        f"Осталось: {remains}\n"
        f"Начало: {start_count}\n"
        f"Списано: {charge}\n\n"
        f"Панель: {html.escape(panel['name'])}"
    )


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
        "📋 <b>Новый заказ продвижения</b>\n\n"
        "Шаг 1/5: введите <b>ключевое слово</b> для поиска в Telegram\n"
        "(например: <code>крипто боты</code>)\n\n"
        "<i>Отмена: /cancel</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PromoCb.filter(F.action == "orders"), PromoOrderFSM.keyword)
async def fsm_order_cancel_to_orders(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    await cb_promo_orders(callback, PromoCb(action="orders"), pool)


@router.message(PromoOrderFSM.keyword)
async def fsm_order_keyword(message: Message, state: FSMContext) -> None:
    kw = message.text.strip() if message.text else ""
    if len(kw) < 2:
        await message.answer("❌ Минимум 2 символа. Введите ключевое слово.")
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
    try:
        pos = int(callback.data.split("_")[-1])
    except ValueError:
        return
    await state.update_data(target_position=pos)
    await state.set_state(PromoOrderFSM.pick_bot)

    bots = await db.warehouse_list_bots(pool, callback.from_user.id, limit=30)
    ready = [b for b in bots if b["status"] == "ready"]

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без бота", callback_data="promo_bot_0")
    for b in ready:
        kb.button(text=f"@{b['bot_username']}", callback_data=f"promo_bot_{b['id']}")
    kb.adjust(1)

    bot_hint = f"✅ Готовых ботов: {len(ready)}" if ready else "⚠️ Нет готовых ботов (добавьте в Складе)"
    await safe_edit(
        callback.message,
        f"✅ Позиция: топ-{pos}\n\n"
        f"Шаг 3/5: выберите <b>бота</b> для продвижения\n{bot_hint}:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("promo_bot_"), PromoOrderFSM.pick_bot)
async def fsm_order_pick_bot(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    try:
        bot_id = int(callback.data.split("_")[-1])
    except ValueError:
        return
    if bot_id == 0:
        bot_id = None
    await state.update_data(bot_id=bot_id)
    await state.set_state(PromoOrderFSM.pick_panel)

    panels = await db.smm_list_panels(pool, callback.from_user.id)
    active_panels = [p for p in panels if p["is_active"]]

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без панели", callback_data="promo_panel_0")
    for p in active_panels:
        balance_str = f" 💰{p['balance']}" if p["balance"] else ""
        kb.button(text=f"{p['name']}{balance_str}", callback_data=f"promo_panel_{p['id']}")
    kb.adjust(1)

    bot_label = "не выбран"
    if bot_id:
        b = await db.warehouse_get_bot(pool, bot_id)
        if b:
            bot_label = f"@{b['bot_username']}"

    await safe_edit(
        callback.message,
        f"✅ Бот: {bot_label}\n\n"
        "Шаг 4/5: выберите <b>SMM-панель</b> для накрутки:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("promo_panel_"), PromoOrderFSM.pick_panel)
async def fsm_order_pick_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        panel_id = int(callback.data.split("_")[-1])
    except ValueError:
        return
    if panel_id == 0:
        panel_id = None
    await state.update_data(panel_id=panel_id)
    await state.set_state(PromoOrderFSM.target_subs)
    await safe_edit(
        callback.message,
        "Шаг 5/5: введите <b>целевое количество подписчиков</b>\n"
        "(например: <code>5000</code>)\n\nОтправьте <code>0</code> чтобы задать позже.",
    )


@router.message(PromoOrderFSM.target_subs)
async def fsm_order_target_subs(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    try:
        subs = int((message.text or "0").strip())
    except ValueError:
        await message.answer("❌ Введите число (количество подписчиков).")
        return

    data = await state.get_data()
    await state.update_data(target_subs=subs if subs > 0 else None)

    # Resolve names for confirmation
    bot_name = "не выбран"
    if data.get("bot_id"):
        b = await db.warehouse_get_bot(pool, data["bot_id"])
        if b:
            bot_name = f"@{b['bot_username']}"

    panel_name = "не выбрана"
    if data.get("panel_id"):
        p = await db.smm_get_panel(pool, data["panel_id"])
        if p:
            panel_name = p["name"]

    text = (
        "📋 <b>Подтверждение заказа</b>\n\n"
        f"🔑 Ключевое слово: <code>{html.escape(data['keyword'])}</code>\n"
        f"🎯 Позиция: топ-{data['target_position']}\n"
        f"🤖 Бот: {html.escape(bot_name)}\n"
        f"📡 Панель: {html.escape(panel_name)}\n"
        f"📊 Цель подписчиков: {subs if subs > 0 else 'не задана'}\n"
    )
    if data.get("bot_id") and data.get("panel_id") and subs > 0:
        text += "\n✅ Готово к запуску накрутки!"
    elif not data.get("bot_id") or not data.get("panel_id"):
        text += "\n⚠️ Бот и панель нужны для автозапуска накрутки."

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
    await db.promo_log(
        pool, callback.from_user.id, "scheduler",
        f"Заказ #{order_id} создан: {data['keyword']}", order_id=order_id
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Открыть заказ", callback_data=PromoCb(action="order_detail", item_id=order_id))
    kb.button(text="◀️ Заказы", callback_data=PromoCb(action="orders"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Заказ #{order_id} создан!</b>\n\n"
        f"🔑 Ключевое слово: <code>{html.escape(data['keyword'])}</code>\n"
        f"🎯 Позиция: топ-{data['target_position']}\n\n"
        + (
            "Заказ готов к запуску накрутки — нажмите «Открыть заказ» → 🚀 Запустить накрутку."
            if data.get("bot_id") and data.get("panel_id") and data.get("target_subs")
            else "Настройте бота и SMM-панель для запуска накрутки."
        ),
        reply_markup=kb.as_markup(),
    )


# ── Bot Warehouse ──────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "warehouse"))
async def cb_warehouse(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    status_filter = callback_data.value
    page = callback_data.page

    updated = await db.warehouse_refresh_statuses(pool)

    bots = await db.warehouse_list_bots(pool, user_id, status=status_filter, limit=10, offset=page * 10)
    all_bots = await db.warehouse_list_bots(pool, user_id, limit=500)

    counts: dict[str, int] = {}
    for b in all_bots:
        counts[b["status"]] = counts.get(b["status"], 0) + 1

    status_line = " · ".join(
        f"{_BOT_STATUS.get(s, s)}: {c}" for s, c in sorted(counts.items())
    ) or "пусто"

    header = f"🤖 <b>Склад ботов</b>\n\n{status_line}\n"
    if updated:
        header += f"<i>✅ {updated} бот(а) созрели → готовы к работе</i>\n"

    if not bots:
        text = header + "\nБотов нет. Добавьте или спарсите из BotFather."
    else:
        lines = [header]
        for b in bots:
            st = _BOT_STATUS.get(b["status"], b["status"])
            age = _days_left(b["ready_at"]) if b["status"] == "aging" else ""
            lines.append(
                f"{st} @{html.escape(b['bot_username'])}{age}\n"
                f"   👥 {b['current_subs']} подп. · рег. {_fmt_dt(b['registered_at'])}\n"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить бота", callback_data=PromoCb(action="bot_add"))
    kb.button(text="🤖 Парсить BotFather", callback_data=PromoCb(action="bot_parse"))
    # Status filter
    for s, label in [("aging", "🕐"), ("ready", "✅"), ("working", "🚀"), ("topped", "🏆"), ("banned", "🚫")]:
        mark = "•" if status_filter == s else ""
        kb.button(text=f"{mark}{label}", callback_data=PromoCb(action="warehouse", value=s))
    if status_filter:
        kb.button(text="Все", callback_data=PromoCb(action="warehouse"))
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
    kb.adjust(2, 5, 1, 1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


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
    status_transitions = {
        "ready": ["working"],
        "working": ["topped", "ready"],
        "topped": ["transferred"],
        "banned": ["ready"],
    }
    for next_st in status_transitions.get(bot["status"], []):
        label = _BOT_STATUS.get(next_st, next_st)
        kb.button(
            text=f"→ {label}",
            callback_data=PromoCb(action="bot_setstatus", item_id=bot["id"], value=next_st),
        )
    if bot["status"] not in ("transferred", "banned"):
        kb.button(
            text="📤 Передать бота",
            callback_data=PromoCb(action="bot_transfer", item_id=bot["id"]),
        )
    kb.button(text="🗑 Удалить", callback_data=PromoCb(action="bot_delete", item_id=bot["id"]))
    kb.button(text="◀️ Склад", callback_data=PromoCb(action="warehouse"))
    kb.adjust(2, 1, 1, 1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "bot_setstatus"))
async def cb_bot_setstatus(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    new_status = callback_data.value
    if new_status not in ("ready", "working", "topped", "transferred", "banned"):
        await callback.answer("Недопустимый статус", show_alert=True)
        return
    bot = await db.warehouse_get_bot(pool, callback_data.item_id)
    if not bot or bot["owner_id"] != callback.from_user.id:
        await callback.answer("Бот не найден", show_alert=True)
        return
    await db.warehouse_update_bot(pool, bot["id"], callback.from_user.id, status=new_status)
    await db.promo_log(pool, callback.from_user.id, "scheduler",
                       f"Бот @{bot['bot_username']} → {new_status}")
    await callback.answer(f"Статус: {_BOT_STATUS.get(new_status, new_status)} ✓", show_alert=True)
    await cb_bot_detail(callback, PromoCb(action="bot_detail", item_id=bot["id"]), pool)


@router.callback_query(PromoCb.filter(F.action == "bot_delete"))
async def cb_bot_delete(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    bot = await db.warehouse_get_bot(pool, callback_data.item_id)
    if not bot or bot["owner_id"] != callback.from_user.id:
        await callback.answer("Бот не найден", show_alert=True)
        return
    await db.warehouse_delete_bot(pool, bot["id"], callback.from_user.id)
    await callback.answer(f"@{bot['bot_username']} удалён ✓", show_alert=True)
    await cb_warehouse(callback, PromoCb(action="warehouse"), pool)


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
        "🤖 <b>Добавить бота на склад</b>\n\n"
        "Введите <b>username</b> бота (без @):\n\n<i>Отмена: /cancel</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PromoCb.filter(F.action == "warehouse"), PromoAddBotFSM.username)
async def fsm_bot_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    await cb_warehouse(callback, PromoCb(action="warehouse"), pool)


@router.message(PromoAddBotFSM.username)
async def fsm_bot_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip().lstrip("@")
    if not username or len(username) < 3:
        await message.answer("❌ Username минимум 3 символа.")
        return
    await state.update_data(username=username)
    await state.set_state(PromoAddBotFSM.reg_date)
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня", callback_data="promo_regdate_today")
    await message.answer(
        f"✅ Username: @{html.escape(username)}\n\n"
        "Введите <b>дату регистрации</b> бота в формате <code>ГГГГ-ММ-ДД</code>\n"
        "или нажмите «Сегодня»:\n\n"
        "<i>Счётчик 21 день запустится с этой даты.</i>",
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
    text = (message.text or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Формат: ГГГГ-ММ-ДД (например: 2025-01-15).")
        return
    await state.update_data(reg_date=text)
    await state.set_state(PromoAddBotFSM.token)
    await message.answer(
        f"✅ Дата: {text}\n\n"
        "Введите <b>токен бота</b> (из BotFather) или <code>-</code> чтобы пропустить:"
    )


@router.message(PromoAddBotFSM.token)
async def fsm_bot_token(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    await state.update_data(token=None if token == "-" else token)
    await state.set_state(PromoAddBotFSM.notes)
    await message.answer("Введите <b>заметки</b> или <code>-</code> чтобы пропустить:")


@router.message(PromoAddBotFSM.notes)
async def fsm_bot_notes(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    notes = (message.text or "").strip()
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
    kb.button(text="🤖 Склад ботов", callback_data=PromoCb(action="warehouse"))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Бот @{html.escape(data['username'])} добавлен!</b>\n\n"
        f"Статус: {_BOT_STATUS.get(bot_status, bot_status)}{days_hint}\n"
        f"Готов к работе: {_fmt_dt(ready_at)}",
        reply_markup=kb.as_markup(),
    )


# ── BotFather parser ───────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "bot_parse"))
async def cb_bot_parse(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Парсю ботов из BotFather...")

    # Select best account for the user
    try:
        from services.resource_selector import select_account
        acc = await select_account(pool, callback.from_user.id, action="read")
    except Exception as exc:
        await callback.message.answer(
            f"⚠️ Нет доступных аккаунтов для парсинга BotFather.\n"
            f"Добавьте аккаунт в /accounts и попробуйте снова.\n"
            f"<code>{html.escape(str(exc)[:200])}</code>"
        )
        return

    if not acc:
        await callback.message.answer(
            "⚠️ Нет подходящего аккаунта для парсинга BotFather.\n"
            "Добавьте аккаунт в /accounts."
        )
        return

    try:
        from services import account_manager
        result = await account_manager.list_bots_via_botfather(
            acc["session_str"], _acc=dict(acc)
        )
    except Exception as exc:
        await callback.message.answer(
            f"⚠️ Ошибка парсинга BotFather:\n<code>{html.escape(str(exc)[:300])}</code>"
        )
        return

    if result.get("error"):
        await callback.message.answer(
            f"❌ BotFather ответил ошибкой:\n<code>{html.escape(str(result['error'])[:300])}</code>"
        )
        return

    bots_found = result.get("bots", [])
    if not bots_found:
        await callback.message.answer(
            "ℹ️ У этого аккаунта нет ботов в BotFather, или список пуст."
        )
        return

    added = 0
    skipped = 0
    for b in bots_found:
        uname = b.get("username", "").lstrip("@")
        if not uname:
            continue
        # Check if already in warehouse
        existing = await pool.fetch(
            "SELECT id FROM bot_warehouse WHERE owner_id=$1 AND LOWER(bot_username)=LOWER($2)",
            callback.from_user.id, uname
        )
        if existing:
            skipped += 1
            continue
        await db.warehouse_add_bot(
            pool,
            owner_id=callback.from_user.id,
            bot_username=uname,
            bot_token_enc=b.get("token"),
            registered_at=datetime.now(tz=timezone.utc),
            notes="спарсен из BotFather",
        )
        added += 1

    await db.promo_log(pool, callback.from_user.id, "parser",
                       f"BotFather parser: найдено {len(bots_found)}, добавлено {added}, пропущено {skipped}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Склад ботов", callback_data=PromoCb(action="warehouse"))
    kb.adjust(1)
    await callback.message.answer(
        f"✅ <b>Парсинг BotFather завершён</b>\n\n"
        f"Найдено ботов: {len(bots_found)}\n"
        f"Добавлено на склад: {added}\n"
        f"Уже было на складе: {skipped}\n\n"
        f"<i>Боты добавлены со статусом «Созревает» (дата рег. = сегодня).\n"
        f"Если боты старше 21 дня — обновите дату в деталях бота.</i>",
        reply_markup=kb.as_markup(),
    )


# ── Bot transfer via BotFather ─────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "bot_transfer"))
async def cb_bot_transfer_start(callback: CallbackQuery, callback_data: PromoCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    bot = await db.warehouse_get_bot(pool, callback_data.item_id)
    if not bot or bot["owner_id"] != callback.from_user.id:
        await callback.answer("Бот не найден", show_alert=True)
        return
    await state.set_state(PromoTransferFSM.new_owner)
    await state.update_data(bot_id=bot["id"], bot_username=bot["bot_username"])
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="bot_detail", item_id=bot["id"]))
    await safe_edit(
        callback.message,
        f"📤 <b>Передача @{html.escape(bot['bot_username'])}</b>\n\n"
        "Введите <b>@username нового владельца</b> (должен принять запрос в BotFather):\n\n"
        "<i>Отмена: /cancel</i>",
        reply_markup=kb.as_markup(),
    )


@router.message(PromoTransferFSM.new_owner)
async def fsm_transfer_new_owner(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    new_owner = (message.text or "").strip().lstrip("@")
    if not new_owner or len(new_owner) < 3:
        await message.answer("❌ Введите корректный @username.")
        return

    data = await state.get_data()
    await state.clear()

    bot_username = data.get("bot_username", "")
    bot_id = data.get("bot_id", 0)

    # Select account for BotFather interaction
    try:
        from services.resource_selector import select_account
        acc = await select_account(pool, message.from_user.id, action="read")
    except Exception as exc:
        await message.answer(
            f"⚠️ Нет доступных аккаунтов.\n<code>{html.escape(str(exc)[:200])}</code>"
        )
        return

    if not acc:
        await message.answer(
            "⚠️ Нет подходящего аккаунта для BotFather.\nДобавьте аккаунт в /accounts."
        )
        return

    await message.answer(
        f"⏳ Запускаю передачу @{html.escape(bot_username)} → @{html.escape(new_owner)} через BotFather..."
    )

    try:
        from services import account_manager
        result = await account_manager.transfer_bot_via_botfather(
            acc["session_str"],
            bot_username=bot_username,
            new_owner_username=new_owner,
            _acc=dict(acc),
        )
    except Exception as exc:
        await message.answer(
            f"⚠️ Ошибка BotFather:\n<code>{html.escape(str(exc)[:300])}</code>"
        )
        return

    if result.get("error"):
        await db.promo_log(pool, message.from_user.id, "transfer",
                           f"Ошибка передачи @{bot_username}: {result['error']}", level="ERROR")
        await message.answer(
            f"❌ <b>Ошибка передачи</b>\n\n"
            f"Бот: @{html.escape(bot_username)}\n"
            f"Ошибка: <code>{html.escape(str(result['error'])[:300])}</code>\n\n"
            f"Возможные причины:\n"
            f"• @{new_owner} не существует\n"
            f"• Новый владелец не принял условия BotFather\n"
            f"• FloodWait — попробуйте позже"
        )
        return

    await db.warehouse_update_bot(pool, bot_id, message.from_user.id, status="transferred")
    await db.promo_log(pool, message.from_user.id, "transfer",
                       f"Бот @{bot_username} передан @{new_owner}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Склад ботов", callback_data=PromoCb(action="warehouse"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Бот @{html.escape(bot_username)} передан!</b>\n\n"
        f"Новый владелец: @{html.escape(new_owner)}\n"
        f"Ответ BotFather: {html.escape(result.get('message', 'успех')[:200])}\n\n"
        f"<i>Бот помечен как «Передан» в складе.</i>",
        reply_markup=kb.as_markup(),
    )


# ── SMM Panels ─────────────────────────────────────────────────────────────────

@router.callback_query(PromoCb.filter(F.action == "panels"))
async def cb_panels(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    panels = await db.smm_list_panels(pool, callback.from_user.id)

    if not panels:
        text = (
            "📡 <b>SMM-панели</b>\n\n"
            "Панели не добавлены.\n\n"
            "Поддерживается любая панель с API v2 (GlobalSMM, SmmRaja, PerfectPanel, "
            "FastSMM, SmmKings и другие — любая панель с API-доступом)."
        )
    else:
        lines = ["📡 <b>SMM-панели</b>\n"]
        for p in panels:
            active_mark = "✅" if p["is_active"] else "⛔"
            balance_str = f" · 💰{p['balance']}" if p["balance"] else ""
            svc_str = f" · сервис #{p['service_id']}" if p["service_id"] else ""
            last_check = _fmt_dt(p["last_checked"]) if p["last_checked"] else "не проверялась"
            lines.append(
                f"{active_mark} <b>{html.escape(p['name'])}</b>{balance_str}{svc_str}\n"
                f"   {html.escape(p['api_url'][:55])}\n"
                f"   Проверена: {last_check}\n"
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
    await safe_edit(callback, text, reply_markup=kb.as_markup())


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
        f"ID сервиса: <code>{html.escape(panel['service_id'] or '—')}</code>\n"
        f"Активна: {'✅ да' if panel['is_active'] else '⛔ нет'}\n"
        f"💰 Баланс: {panel['balance'] or '—'}\n"
        f"🕒 Проверена: {_fmt_dt(panel['last_checked'])}\n"
        f"📅 Добавлена: {_fmt_dt(panel['created_at'])}\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить баланс", callback_data=PromoCb(action="panel_check", item_id=panel["id"]))
    kb.button(text="📋 Список сервисов", callback_data=PromoCb(action="panel_services", item_id=panel["id"]))
    toggle_label = "⛔ Деактивировать" if panel["is_active"] else "✅ Активировать"
    kb.button(text=toggle_label, callback_data=PromoCb(action="panel_toggle", item_id=panel["id"]))
    kb.button(text="🗑 Удалить", callback_data=PromoCb(action="panel_delete", item_id=panel["id"]))
    kb.button(text="◀️ Панели", callback_data=PromoCb(action="panels"))
    kb.adjust(2, 1, 1, 1)
    await safe_edit(callback, text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "panel_check"))
async def cb_panel_check(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Проверяю баланс...")
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        await callback.answer("Панель не найдена", show_alert=True)
        return

    client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
    result = await client.get_balance()

    if result.get("error"):
        await callback.message.answer(
            f"⚠️ <b>Ошибка подключения к панели</b>\n\n"
            f"Панель: {html.escape(panel['name'])}\n"
            f"Ошибка: <code>{html.escape(str(result['error'])[:300])}</code>\n\n"
            f"Проверьте URL и API-ключ в настройках панели."
        )
        return

    balance = result.get("balance", result.get("Balance", "?"))
    currency = result.get("currency", result.get("Currency", ""))

    now = datetime.now(tz=timezone.utc)
    try:
        balance_float = float(str(balance).replace(",", "."))
    except (TypeError, ValueError):
        balance_float = 0.0

    await db.smm_update_panel(
        pool, panel["id"], callback.from_user.id,
        balance=balance_float, last_checked=now,
    )
    await db.promo_log(pool, callback.from_user.id, "booster",
                       f"Панель {panel['name']}: баланс {balance} {currency}")

    await callback.answer(f"💰 Баланс: {balance} {currency}", show_alert=True)
    await cb_panel_detail(callback, PromoCb(action="panel_detail", item_id=panel["id"]), pool)


@router.callback_query(PromoCb.filter(F.action == "panel_services"))
async def cb_panel_services(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Загружаю список сервисов...")
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        await callback.answer("Панель не найдена", show_alert=True)
        return

    client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
    services = await client.get_services()

    if not services:
        await callback.message.answer(
            f"⚠️ Список сервисов пуст или ошибка запроса.\n"
            f"Панель: {html.escape(panel['name'])}"
        )
        return

    # Filter Telegram services
    tg_services = [
        s for s in services
        if isinstance(s, dict) and "telegram" in str(s.get("name", "") + str(s.get("category", ""))).lower()
    ]
    show = tg_services[:30] if tg_services else services[:30]

    lines = [f"📋 <b>Сервисы панели «{html.escape(panel['name'])}»</b>"]
    if tg_services:
        lines.append(f"<i>Telegram-сервисов: {len(tg_services)} из {len(services)}</i>\n")
    else:
        lines.append(f"<i>Всего сервисов: {len(services)} (показаны первые 30)</i>\n")

    for s in show:
        svc_id = s.get("service", s.get("id", "?"))
        name = s.get("name", "?")[:60]
        rate = s.get("rate", "?")
        min_q = s.get("min", "?")
        max_q = s.get("max", "?")
        lines.append(
            f"<b>#{svc_id}</b> {html.escape(name)}\n"
            f"   💰 {rate}/1000 · мин {min_q} / макс {max_q}\n"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n...<i>список обрезан</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К панели", callback_data=PromoCb(action="panel_detail", item_id=panel["id"]))
    await callback.message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(PromoCb.filter(F.action == "panel_toggle"))
async def cb_panel_toggle(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        await callback.answer("Панель не найдена", show_alert=True)
        return
    new_active = not panel["is_active"]
    await db.smm_update_panel(pool, panel["id"], callback.from_user.id, is_active=new_active)
    word = "активирована" if new_active else "деактивирована"
    await callback.answer(f"Панель {word} ✓", show_alert=True)
    await cb_panel_detail(callback, PromoCb(action="panel_detail", item_id=panel["id"]), pool)


@router.callback_query(PromoCb.filter(F.action == "panel_delete"))
async def cb_panel_delete(callback: CallbackQuery, callback_data: PromoCb, pool: asyncpg.Pool) -> None:
    panel = await db.smm_get_panel(pool, callback_data.item_id)
    if not panel or panel["owner_id"] != callback.from_user.id:
        await callback.answer("Панель не найдена", show_alert=True)
        return
    await db.smm_delete_panel(pool, panel["id"], callback.from_user.id)
    await callback.answer(f"Панель «{panel['name']}» удалена ✓", show_alert=True)
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
        "📡 <b>Добавить SMM-панель</b>\n\n"
        "Поддерживается любая панель с API v2:\n"
        "GlobalSMM, SmmRaja, PerfectPanel, FastSMM, SmmKings и другие\n\n"
        "Шаг 1/4: введите <b>название</b> (например: <code>GlobalSMM</code>):\n\n"
        "<i>Отмена: /cancel</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PromoCb.filter(F.action == "panels"), PromoAddPanelFSM.name)
async def fsm_panel_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    await cb_panels(callback, pool)


@router.message(PromoAddPanelFSM.name)
async def fsm_panel_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("❌ Введите название панели.")
        return
    await state.update_data(name=name)
    await state.set_state(PromoAddPanelFSM.api_url)
    await message.answer(
        f"✅ Название: <b>{html.escape(name)}</b>\n\n"
        "Шаг 2/4: введите <b>URL API</b> панели\n"
        "(например: <code>https://globalssmm.com/api/v2</code>):"
    )


@router.message(PromoAddPanelFSM.api_url)
async def fsm_panel_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith("http"):
        await message.answer("❌ URL должен начинаться с http:// или https://")
        return
    await state.update_data(api_url=url)
    await state.set_state(PromoAddPanelFSM.api_key)
    await message.answer("Шаг 3/4: введите <b>API-ключ</b> панели:")


@router.message(PromoAddPanelFSM.api_key)
async def fsm_panel_key(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    if len(key) < 5:
        await message.answer("❌ API-ключ слишком короткий.")
        return
    await state.update_data(api_key=key)
    data = await state.get_data()

    # Test connection immediately
    await message.answer("⏳ Тестирую подключение к панели...")
    client = smm_svc.make_client(data["api_url"], key)
    test_result = await client.get_balance()

    if test_result.get("error"):
        kb = InlineKeyboardBuilder()
        kb.button(text="↩️ Ввести другой ключ", callback_data="promo_panel_rekey")
        kb.button(text="⏭ Всё равно продолжить", callback_data="promo_panel_force")
        kb.adjust(1)
        await message.answer(
            f"⚠️ <b>Тест соединения не прошёл</b>\n\n"
            f"Ошибка: <code>{html.escape(str(test_result['error'])[:300])}</code>\n\n"
            f"Проверьте URL и ключ, или продолжите сохранение без верификации.",
            reply_markup=kb.as_markup(),
        )
        return

    balance = test_result.get("balance", test_result.get("Balance", "?"))
    currency = test_result.get("currency", test_result.get("Currency", ""))
    await message.answer(
        f"✅ <b>Соединение успешно!</b>\n💰 Баланс: {balance} {currency}\n\n"
        "Шаг 4/4: введите <b>ID сервиса</b> для накрутки Telegram-подписчиков\n"
        "(найдите нужный сервис через «Список сервисов» после добавления)\n"
        "или отправьте <code>-</code> чтобы пропустить:"
    )
    await state.update_data(balance=balance, balance_verified=True)
    await state.set_state(PromoAddPanelFSM.service_id)


@router.callback_query(F.data == "promo_panel_rekey", PromoAddPanelFSM.api_key)
async def fsm_panel_rekey(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    await safe_edit(
        callback.message,
        f"Шаг 3/4: введите <b>API-ключ</b> панели заново:\n(URL: {html.escape(data.get('api_url', '')[:60])})"
    )


@router.callback_query(F.data == "promo_panel_force", PromoAddPanelFSM.api_key)
async def fsm_panel_force(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PromoAddPanelFSM.service_id)
    await safe_edit(
        callback.message,
        "Шаг 4/4: введите <b>ID сервиса</b> для накрутки Telegram-подписчиков\n"
        "или <code>-</code> чтобы пропустить:"
    )


@router.message(PromoAddPanelFSM.service_id)
async def fsm_panel_service_id(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    svc_id = (message.text or "").strip()
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
    kb.button(text="📡 Список панелей", callback_data=PromoCb(action="panels"))
    kb.button(text="📋 Сервисы панели", callback_data=PromoCb(action="panel_services", item_id=panel_id))
    kb.adjust(1)
    verified_str = " ✅ (баланс проверен)" if data.get("balance_verified") else ""
    await message.answer(
        f"✅ <b>Панель «{html.escape(data['name'])}» добавлена!{verified_str}</b>\n\n"
        + (f"💰 Баланс: {data.get('balance', '?')}\n" if data.get("balance_verified") else "")
        + (f"🎯 ID сервиса: <code>{svc_id}</code>\n" if svc_id else "⚠️ ID сервиса не задан — задайте через «Сервисы панели»\n")
        + "\nНажмите «Сервисы панели» чтобы найти нужный Telegram-сервис.",
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
        "<i>Данные берутся из трекера позиций. Для активного мониторинга добавьте "
        "бота в /ranking.</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(PromoCb.filter(F.action == "menu"), PromoTopCheckFSM.keyword)
async def fsm_topcheck_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await state.clear()
    await _show_menu(callback, pool, edit=True)


@router.message(PromoTopCheckFSM.keyword)
async def fsm_topcheck_keyword(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    keyword = (message.text or "").strip()
    if not keyword:
        await message.answer("❌ Введите ключевое слово.")
        return
    await state.clear()

    await db.promo_log(pool, message.from_user.id, "checker",
                       f"Запрос чекера топа: {keyword}")

    try:
        rows = await pool.fetch(
            """SELECT k.keyword, k.target_username, r.position, r.checked_at
               FROM ranking_keywords k
               LEFT JOIN ranking_results r ON r.keyword_id = k.id
               WHERE LOWER(k.keyword) = LOWER($1)
               ORDER BY r.checked_at DESC LIMIT 10""",
            keyword,
        )
    except Exception:
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Трекер позиций", callback_data=RankCb(action="menu"))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)

    if rows:
        # Group by username, get latest position for each
        seen: dict[str, dict] = {}
        for r in rows:
            uname = r["target_username"] or "?"
            if uname not in seen:
                seen[uname] = dict(r)
        lines = [f"🔍 <b>Топ по запросу: «{html.escape(keyword)}»</b>\n"]
        for i, (uname, r) in enumerate(seen.items(), 1):
            pos = f"#{r['position']}" if r["position"] else "н/д"
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
            lines.append(
                f"{medal} @{html.escape(uname)} — позиция {pos}\n"
                f"   Проверено: {_fmt_dt(r['checked_at'])}"
            )
        text = "\n".join(lines)
    else:
        text = (
            f"🔍 <b>Чекер топа: «{html.escape(keyword)}»</b>\n\n"
            "По этому ключевому слову нет данных в трекере.\n\n"
            "Добавьте бота и ключевое слово в <b>Трекер позиций (/ranking)</b> — "
            "система будет автоматически отслеживать позиции в поиске Telegram."
        )
    await message.answer(text, reply_markup=kb.as_markup())


# ── Session upload ─────────────────────────────────────────────────────────────

_SESSION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sessions")


@router.callback_query(PromoCb.filter(F.action == "session_upload"))
async def cb_session_upload_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text("🔒 <b>Менеджер сессий — 💎 ПОДПИСКА</b>\n\nОформите: /subscription")
        return
    await state.set_state(PromoSessionUploadFSM.waiting_file)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PromoCb(action="menu"))
    await safe_edit(
        callback.message,
        "📁 <b>Загрузить .session файл</b>\n\n"
        "Отправьте <b>файл</b> с расширением <code>.session</code>\n\n"
        "Поддерживаемые форматы:\n"
        "• Telethon (.session)\n"
        "• Pyrogram (.session)\n\n"
        "После загрузки сессия будет сохранена и её можно привязать к боту в складе.\n\n"
        "<i>Отмена: /cancel</i>",
        reply_markup=kb.as_markup(),
    )


@router.message(PromoSessionUploadFSM.waiting_file)
async def fsm_session_file(message: Message, state: FSMContext, pool: asyncpg.Pool, bot: Bot) -> None:
    doc: Document | None = message.document
    if not doc:
        await message.answer("❌ Отправьте файл .session (не текст, а именно файл).")
        return

    fname = doc.file_name or ""
    if not fname.endswith(".session"):
        await message.answer(f"❌ Файл должен иметь расширение .session, получен: {html.escape(fname)}")
        return

    await state.clear()

    # Ensure sessions directory exists
    try:
        os.makedirs(_SESSION_DIR, exist_ok=True)
    except OSError:
        pass

    safe_name = f"{message.from_user.id}_{fname.replace('/', '_').replace('..', '_')}"
    dest = os.path.join(_SESSION_DIR, safe_name)

    try:
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, dest)
    except Exception as exc:
        await message.answer(
            f"❌ Ошибка загрузки файла:\n<code>{html.escape(str(exc)[:200])}</code>"
        )
        return

    await db.promo_log(pool, message.from_user.id, "autoreg",
                       f"Сессия загружена: {fname} → {safe_name}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Привязать к боту", callback_data=PromoCb(action="warehouse"))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Сессия загружена!</b>\n\n"
        f"Файл: <code>{html.escape(fname)}</code>\n"
        f"Сохранён как: <code>{html.escape(safe_name)}</code>\n\n"
        "Привяжите сессию к боту на складе: выберите бота → редактировать → указать путь к сессии.\n\n"
        f"<i>Путь: sessions/{html.escape(safe_name)}</i>",
        reply_markup=kb.as_markup(),
    )


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

    if order_id:
        title = f"📜 <b>Логи заказа #{order_id}</b>"
    else:
        title = "📜 <b>Логи платформы</b>"

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
    for lvl, icon in [("INFO", "ℹ️"), ("WARN", "⚠️"), ("ERROR", "❌")]:
        mark = "•" if level_filter == lvl else ""
        kb.button(
            text=f"{mark}{icon}",
            callback_data=PromoCb(action="logs", item_id=order_id or 0, value=lvl),
        )
    if level_filter:
        kb.button(text="Все", callback_data=PromoCb(action="logs", item_id=order_id or 0))
    if order_id:
        kb.button(text="◀️ Заказ", callback_data=PromoCb(action="order_detail", item_id=order_id))
    kb.button(text="◀️ Платформа", callback_data=PromoCb(action="menu"))
    kb.adjust(3, 1, 1, 1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())
