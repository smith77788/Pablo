"""Host-Server — покупаемый модуль аренды инфраструктуры (бот-UI).

Модульный хендлер (как strike.py) — не трогает index.html. Вся логика в
services/host_server.py; здесь только экраны и роутинг колбэков.

Сценарии: купить модуль → сдать своё (предложение) / взять чужое (аренда) /
с согласия отдать вычислительные мощности устройства (kind=device_compute).
"""

from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import HostCb, BmCb
from bot.states import HostOfferingFSM, HostPriceFSM
from bot.utils.op_helpers import safe_answer
from bot.utils.subscription import is_platform_admin
from services import host_server as hs
from services.logger import log_exc_swallow

router = Router(name="host_server")
log = logging.getLogger(__name__)

_KIND_LABELS = {
    "proxy": "🌐 Прокси/канал",
    "server": "🖥️ Сервер/VPS",
    "device_compute": "⚙️ Мощности устройства",
}
_PERIOD_LABELS = {"hour": "в час", "day": "в день", "month": "в месяц"}
_STATUS_LABELS = {
    "pending": "⏳ ожидает", "active": "✅ активна", "ended": "🏁 завершена",
    "cancelled": "🚫 отменена", "rejected": "⛔ отклонена",
}


def _fmt_price(price, period: str) -> str:
    try:
        p = f"{float(price):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        p = str(price)
    return f"${p} {_PERIOD_LABELS.get(period, period)}"


# ── меню ──────────────────────────────────────────────────────────────────────

def _menu_kb(has_access: bool, is_admin: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if has_access:
        kb.button(text="🛒 Маркетплейс (взять в аренду)", callback_data=HostCb(action="market"))
        kb.button(text="➕ Сдать в аренду", callback_data=HostCb(action="new"))
        kb.button(text="📦 Мои предложения и аренды", callback_data=HostCb(action="my"))
    else:
        kb.button(text="💳 Купить модуль", callback_data=HostCb(action="buy"))
    if is_admin:
        kb.button(text="💲 Изменить цену модуля", callback_data=HostCb(action="setprice"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(1)
    return kb


@router.callback_query(HostCb.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback)
    uid = callback.from_user.id
    try:
        access = await hs.has_access(pool, uid)
        price = await hs.get_price(pool)
    except Exception:
        log_exc_swallow(log, "host cb_menu")
        access, price = False, hs.DEFAULT_PRICE_USD
    admin = is_platform_admin(uid)
    if access:
        text = (
            "🖥️ <b>Host-Server</b> — активен\n\n"
            "Аренда инфраструктуры: сдавайте свою, берите чужую, "
            "или (по согласию) зарабатывайте на мощностях своего устройства.\n\n"
            "Выберите действие:"
        )
    else:
        text = (
            "🖥️ <b>Host-Server</b>\n\n"
            "Маркетплейс инфраструктуры внутри платформы:\n"
            "• 🏷 <b>Сдавайте</b> свои прокси, серверы или мощности устройства\n"
            "• 🛒 <b>Берите в аренду</b> чужие ресурсы под свои операции\n"
            "• ⚙️ <b>Зарабатывайте</b>, разрешив использовать ваше устройство "
            "под инфраструктуру арендаторов\n\n"
            f"💰 <b>Стоимость доступа:</b> ${price} USDT · пожизненно"
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_menu_kb(access, admin).as_markup())


# ── покупка ─────────────────────────────────────────────────────────────────

@router.callback_query(HostCb.filter(F.action == "buy"))
async def cb_buy(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id
    try:
        res = await hs.create_purchase(pool, uid)
    except Exception:
        log_exc_swallow(log, "host cb_buy")
        await callback.message.edit_text("❌ Ошибка создания платежа. Попробуйте позже.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить оплату", callback_data=HostCb(action="check_pay"))
    kb.button(text="◀️ Назад", callback_data=HostCb(action="menu"))
    kb.adjust(1)
    if res.get("already"):
        await callback.message.edit_text(
            "✅ Модуль уже активен.", parse_mode="HTML", reply_markup=kb.as_markup())
        return
    wallet = res["wallet"]
    price = res["price_usd"]
    if wallet == "NOT_CONFIGURED":
        await callback.message.edit_text(
            f"🖥️ <b>Host-Server — ${price} USDT</b>\n\n"
            "⚠️ Автоматическая оплата не настроена.\n"
            "Свяжитесь с администратором для активации.",
            parse_mode="HTML", reply_markup=kb.as_markup())
        return
    await callback.message.edit_text(
        f"🖥️ <b>Host-Server — оплата</b>\n\n"
        f"Сумма: <b>{price} USDT</b>\n"
        f"Сеть: <b>TRC-20 (TRON)</b>\n\n"
        f"Кошелёк:\n<code>{html.escape(wallet)}</code>\n\n"
        f"Переведите ровно <b>{price} USDT</b> и нажмите «Проверить оплату».\n"
        f"⚠️ Другие сети не принимаются.\n\n"
        f"⏱ Подтверждение: 5–30 минут\n"
        f"<i>ID платежа: {res['reference']}</i>",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(HostCb.filter(F.action == "check_pay"))
async def cb_check_pay(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id
    if await hs.has_access(pool, uid):
        kb = InlineKeyboardBuilder()
        kb.button(text="🖥️ Открыть Host-Server", callback_data=HostCb(action="menu"))
        await callback.message.edit_text(
            "✅ <b>Модуль Host-Server активирован!</b>\n\nДоступ открыт.",
            parse_mode="HTML", reply_markup=kb.as_markup())
        return
    try:
        row = await pool.fetchrow(
            "SELECT status FROM payments WHERE user_id=$1 AND plan='host_server' "
            "ORDER BY created_at DESC LIMIT 1", uid)
    except Exception:
        row = None
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=HostCb(action="check_pay"))
    kb.button(text="◀️ Назад", callback_data=HostCb(action="menu"))
    kb.adjust(1)
    if not row:
        await callback.message.edit_text(
            "❌ Платёж не найден. Создайте новый через «Купить».", reply_markup=kb.as_markup())
        return
    labels = {"pending": "⏳ Ожидает оплаты", "confirming": "🔄 Подтверждается в блокчейне…",
              "confirmed": "✅ Подтверждён", "expired": "❌ Истёк"}
    await callback.message.edit_text(
        f"Статус платежа: <b>{labels.get(row['status'], row['status'])}</b>\n\n"
        "После подтверждения сети доступ откроется автоматически.",
        parse_mode="HTML", reply_markup=kb.as_markup())


# ── маркетплейс: взять в аренду ──────────────────────────────────────────────

@router.callback_query(HostCb.filter(F.action == "market"))
async def cb_market(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id
    if not await hs.has_access(pool, uid):
        await callback.answer("Нужен доступ к модулю.", show_alert=True)
        return
    offers = await hs.list_market(pool, viewer_id=uid, limit=20)
    kb = InlineKeyboardBuilder()
    if not offers:
        text = "🛒 <b>Маркетплейс</b>\n\nПока нет доступных предложений. Загляните позже."
    else:
        lines = ["🛒 <b>Маркетплейс — доступно к аренде</b>\n"]
        for o in offers:
            lines.append(
                f"• {_KIND_LABELS.get(o['kind'], o['kind'])} <b>{html.escape(o['title'])}</b>"
                f" — {_fmt_price(o['price_usd'], o['period'])}"
                + (f" · {html.escape(o['region'])}" if o.get("region") else "")
            )
            kb.button(text=f"Арендовать: {o['title'][:24]}", callback_data=HostCb(action="rent", oid=o["id"]))
        text = "\n".join(lines)
    kb.button(text="◀️ Назад", callback_data=HostCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(HostCb.filter(F.action == "rent"))
async def cb_rent(callback: CallbackQuery, callback_data: HostCb, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id
    if not await hs.has_access(pool, uid):
        await callback.answer("Нужен доступ к модулю.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 К маркетплейсу", callback_data=HostCb(action="market"))
    kb.button(text="📦 Мои аренды", callback_data=HostCb(action="my"))
    kb.adjust(1)
    try:
        r = await hs.rent_offering(pool, uid, callback_data.oid, units=1)
    except ValueError as ve:
        await callback.answer(str(ve), show_alert=True)
        return
    except Exception:
        log_exc_swallow(log, "host cb_rent")
        await callback.message.edit_text("❌ Не удалось оформить аренду.", reply_markup=kb.as_markup())
        return
    await callback.message.edit_text(
        f"✅ <b>Заявка на аренду создана</b>\n\n"
        f"Сумма: <b>{_fmt_price(r['price_usd'], r['period'])}</b>\n"
        f"Статус: {_STATUS_LABELS['pending']} (ждёт подтверждения владельца)\n\n"
        "Как только владелец подтвердит — аренда станет активной.",
        parse_mode="HTML", reply_markup=kb.as_markup())


# ── мои предложения и аренды ─────────────────────────────────────────────────

@router.callback_query(HostCb.filter(F.action == "my"))
async def cb_my(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id
    offerings = await hs.my_offerings(pool, uid)
    incoming = await hs.incoming_rentals(pool, uid)
    outgoing = await hs.my_rentals(pool, uid)
    kb = InlineKeyboardBuilder()
    lines = ["📦 <b>Мои предложения и аренды</b>\n"]

    lines.append("<b>Мои предложения (сдаю):</b>")
    if offerings:
        for o in offerings:
            state = "🟢" if o["is_active"] else "⚪️"
            lines.append(f"{state} {_KIND_LABELS.get(o['kind'], o['kind'])} {html.escape(o['title'])}"
                         f" — {_fmt_price(o['price_usd'], o['period'])}")
            kb.button(
                text=f"{'Снять' if o['is_active'] else 'Включить'}: {o['title'][:20]}",
                callback_data=HostCb(action="toggle", oid=o["id"]))
    else:
        lines.append("<i>нет</i>")

    lines.append("\n<b>Заявки на мои предложения:</b>")
    if incoming:
        for r in incoming:
            lines.append(f"• {html.escape(r['title'])} — {_STATUS_LABELS.get(r['status'], r['status'])}"
                         f" ({_fmt_price(r['price_usd'], r['period'])})")
            if r["status"] == "pending":
                kb.button(text=f"✅ Подтвердить #{r['id']}", callback_data=HostCb(action="approve", rid=r["id"]))
                kb.button(text=f"⛔ Отклонить #{r['id']}", callback_data=HostCb(action="reject", rid=r["id"]))
            elif r["status"] == "active":
                kb.button(text=f"🏁 Завершить #{r['id']}", callback_data=HostCb(action="end", rid=r["id"]))
    else:
        lines.append("<i>нет</i>")

    lines.append("\n<b>Мои аренды (беру):</b>")
    if outgoing:
        for r in outgoing:
            lines.append(f"• {html.escape(r['title'])} — {_STATUS_LABELS.get(r['status'], r['status'])}"
                         f" ({_fmt_price(r['price_usd'], r['period'])})")
            if r["status"] in ("pending", "active"):
                kb.button(text=f"🚫 Отменить #{r['id']}", callback_data=HostCb(action="cancel", rid=r["id"]))
    else:
        lines.append("<i>нет</i>")

    kb.button(text="◀️ Назад", callback_data=HostCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(HostCb.filter(F.action == "toggle"))
async def cb_toggle(callback: CallbackQuery, callback_data: HostCb, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    # определить текущее состояние → инвертировать
    offs = {o["id"]: o for o in await hs.my_offerings(pool, callback.from_user.id)}
    o = offs.get(callback_data.oid)
    if not o:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    await hs.set_offering_active(pool, callback.from_user.id, callback_data.oid, not o["is_active"])
    await cb_my(callback, pool)


async def _rental_action(callback, callback_data, pool, new_status, as_provider):
    try:
        await hs.set_rental_status(pool, callback.from_user.id, callback_data.rid, new_status, as_provider=as_provider)
    except (ValueError, PermissionError) as e:
        await callback.answer(str(e), show_alert=True)
        return
    await cb_my(callback, pool)


@router.callback_query(HostCb.filter(F.action == "approve"))
async def cb_approve(callback: CallbackQuery, callback_data: HostCb, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await _rental_action(callback, callback_data, pool, "active", True)


@router.callback_query(HostCb.filter(F.action == "reject"))
async def cb_reject(callback: CallbackQuery, callback_data: HostCb, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await _rental_action(callback, callback_data, pool, "rejected", True)


@router.callback_query(HostCb.filter(F.action == "end"))
async def cb_end(callback: CallbackQuery, callback_data: HostCb, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await _rental_action(callback, callback_data, pool, "ended", True)


@router.callback_query(HostCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, callback_data: HostCb, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await _rental_action(callback, callback_data, pool, "cancelled", False)


# ── сдать в аренду: FSM создания предложения ─────────────────────────────────

@router.callback_query(HostCb.filter(F.action == "new"))
async def cb_new(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await safe_answer(callback)
    if not await hs.has_access(pool, callback.from_user.id):
        await callback.answer("Нужен доступ к модулю.", show_alert=True)
        return
    await state.set_state(HostOfferingFSM.kind)
    kb = InlineKeyboardBuilder()
    for k, label in _KIND_LABELS.items():
        kb.button(text=label, callback_data=HostCb(action="kind", val=k))
    kb.button(text="❌ Отмена", callback_data=HostCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "➕ <b>Новое предложение — шаг 1/4</b>\n\nЧто сдаёте в аренду?",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(HostOfferingFSM.kind, HostCb.filter(F.action == "kind"))
async def cb_new_kind(callback: CallbackQuery, callback_data: HostCb, state: FSMContext) -> None:
    await safe_answer(callback)
    kind = callback_data.val if callback_data.val in hs.OFFERING_KINDS else "server"
    await state.update_data(kind=kind)
    await state.set_state(HostOfferingFSM.title)
    await callback.message.edit_text(
        f"➕ <b>Шаг 2/4 — {_KIND_LABELS[kind]}</b>\n\n"
        "Введите короткое название предложения (например «VPS 4CPU/8GB Амстердам»):",
        parse_mode="HTML")


@router.message(HostOfferingFSM.title)
async def fsm_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("⚠️ Введите название текстом.")
        return
    await state.update_data(title=title[:200])
    await state.set_state(HostOfferingFSM.price)
    await message.answer(
        "➕ <b>Шаг 3/4</b>\n\nУкажите цену в USD (число), например <code>15</code>:",
        parse_mode="HTML")


@router.message(HostOfferingFSM.price)
async def fsm_price(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        price = round(float(raw), 2)
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("⚠️ Введите цену числом, например 15 или 9.99.")
        return
    await state.update_data(price=price)
    await state.set_state(HostOfferingFSM.period)
    kb = InlineKeyboardBuilder()
    for p, label in _PERIOD_LABELS.items():
        kb.button(text=label.capitalize(), callback_data=HostCb(action="period", val=p))
    kb.adjust(3)
    await message.answer("➕ <b>Шаг 4/4</b>\n\nЗа какой период цена?", parse_mode="HTML",
                         reply_markup=kb.as_markup())


@router.callback_query(HostOfferingFSM.period, HostCb.filter(F.action == "period"))
async def cb_new_period(callback: CallbackQuery, callback_data: HostCb, state: FSMContext, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    period = callback_data.val if callback_data.val in hs.PERIODS else "month"
    await state.update_data(period=period)
    data = await state.get_data()
    # device_compute требует явного согласия — отдельный шаг
    if data.get("kind") == "device_compute":
        await state.set_state(HostOfferingFSM.consent)
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Согласен(на) — публиковать", callback_data=HostCb(action="consent_yes"))
        kb.button(text="❌ Отмена", callback_data=HostCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "⚙️ <b>Согласие на использование устройства</b>\n\n"
            "Публикуя это предложение, вы разрешаете (при активной аренде и "
            "включённом операторе) устанавливать и запускать инфраструктуру "
            "арендаторов на вашем устройстве в обмен на арендную плату.\n\n"
            "Вы можете снять предложение в любой момент.\n\nПодтверждаете согласие?",
            parse_mode="HTML", reply_markup=kb.as_markup())
        return
    await _save_offering(callback, state, pool, consent=False)


@router.callback_query(HostOfferingFSM.consent, HostCb.filter(F.action == "consent_yes"))
async def cb_consent_yes(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await _save_offering(callback, state, pool, consent=True)


async def _save_offering(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, *, consent: bool) -> None:
    data = await state.get_data()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Мои предложения", callback_data=HostCb(action="my"))
    kb.button(text="🖥️ Меню", callback_data=HostCb(action="menu"))
    kb.adjust(1)
    try:
        res = await hs.create_offering(
            pool, callback.from_user.id,
            kind=data.get("kind", "server"),
            title=data.get("title", ""),
            price_usd=data.get("price", 0),
            period=data.get("period", "month"),
            allow_device_compute=consent,
        )
    except (ValueError, PermissionError) as e:
        await callback.message.edit_text(f"❌ {html.escape(str(e))}", reply_markup=kb.as_markup())
        return
    except Exception:
        log_exc_swallow(log, "host _save_offering")
        await callback.message.edit_text("❌ Не удалось создать предложение.", reply_markup=kb.as_markup())
        return
    await callback.message.edit_text(
        f"✅ <b>Предложение опубликовано!</b>\n\n"
        f"{_KIND_LABELS.get(res['kind'], res['kind'])} — {_fmt_price(res['price_usd'], res['period'])}\n"
        "Оно появилось в маркетплейсе для других пользователей.",
        parse_mode="HTML", reply_markup=kb.as_markup())


# ── админ: изменить цену модуля ──────────────────────────────────────────────

@router.callback_query(HostCb.filter(F.action == "setprice"))
async def cb_setprice(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_platform_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    await safe_answer(callback)
    await state.set_state(HostPriceFSM.waiting_price)
    await callback.message.edit_text(
        "💲 <b>Цена модуля Host-Server</b>\n\nВведите новую цену в USD (1–100000):",
        parse_mode="HTML")


@router.message(HostPriceFSM.waiting_price)
async def fsm_setprice(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    if not is_platform_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    kb = InlineKeyboardBuilder()
    kb.button(text="🖥️ Меню", callback_data=HostCb(action="menu"))
    try:
        applied = await hs.set_price(pool, raw)
    except ValueError as ve:
        await message.answer(f"⚠️ {ve}")
        return
    await state.clear()
    await message.answer(
        f"✅ Новая цена модуля: <b>${applied} USDT</b>.", parse_mode="HTML",
        reply_markup=kb.as_markup())
