"""Strike Module — платный модуль массовой зачистки нелегального контента.

Доступ: разовая оплата $250 USDT (TRC-20). Пожизненная лицензия.
Хранит доступ в таблице strike_access.
"""
from __future__ import annotations

import os
import random
import string
import asyncio

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import StrikeCb, ChanCb, BmCb

router = Router(name="strike")

_PRICE_USD = 250


# ── helpers ──────────────────────────────────────────────────────────────────

def _tron_wallet() -> str:
    return os.getenv("TRON_WALLET", "")


def _gen_ref() -> str:
    return "STK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


async def _has_access(pool: asyncpg.Pool, user_id: int) -> bool:
    row = await pool.fetchrow("SELECT 1 FROM strike_access WHERE user_id=$1", user_id)
    return row is not None


def _menu_kb(has_access: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if has_access:
        kb.button(text="🚨 Одиночная цель",    callback_data=ChanCb(action="br_mode_single"))
        kb.button(text="📋 Список целей",       callback_data=ChanCb(action="br_mode_batch"))
        kb.button(text="⚙️ Настройки атаки",   callback_data=StrikeCb(action="settings"))
    else:
        kb.button(text="💳 Купить за $250 USDT", callback_data=StrikeCb(action="buy"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(2, 1) if has_access else kb.adjust(1, 1)
    return kb


# ── main menu ─────────────────────────────────────────────────────────────────

@router.callback_query(StrikeCb.filter(F.action == "menu"))
async def cb_strike_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    access = await _has_access(pool, callback.from_user.id)

    if access:
        text = (
            "⚔️ <b>Strike Module</b> — активен\n\n"
            "<b>8-векторная атака на нелегальный ресурс:</b>\n"
            "① Жалоба на канал (primary reason)\n"
            "② Multi-reason — вторичные причины\n"
            "③ Вход в канал → жалобы изнутри\n"
            "④ Жалобы на 10 последних сообщений\n"
            "⑤ Реакции 👎 на последние посты\n"
            "⑥ Жалобы на всех администраторов\n"
            "⑦ Парсинг и жалобы на связанные боты\n"
            "⑧ Пересылка в @stopCA / @notoscam\n\n"
            "Выберите режим:"
        )
    else:
        text = (
            "⚔️ <b>Strike Module</b>\n\n"
            "<b>Модуль массовой зачистки нелегального контента</b>\n\n"
            "Одним нажатием — запустить скоординированную атаку с "
            "нескольких аккаунтов против:\n"
            "• 🟣 Наркотики и запрещённые вещества\n"
            "• 💣 Терроризм и экстремизм\n"
            "• 🚨 CSAM (детский контент)\n"
            "• 🕸 Даркнет-услуги\n"
            "• 🔫 Торговля оружием\n"
            "• 💸 Мошенничество\n\n"
            "<b>Каждый аккаунт выполняет 8 действий:</b> жалоба на канал, "
            "жалобы на сообщения, жалобы на администраторов, пересылка "
            "в официальные боты Telegram Trust &amp; Safety и другое.\n\n"
            "💰 <b>Стоимость:</b> $250 USDT · Пожизненный доступ"
        )

    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=_menu_kb(access).as_markup(),
    )


# ── settings stub ─────────────────────────────────────────────────────────────

@router.callback_query(StrikeCb.filter(F.action == "settings"))
async def cb_strike_settings(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    access = await _has_access(pool, callback.from_user.id)
    if not access:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
    await callback.message.edit_text(
        "⚙️ <b>Настройки Strike</b>\n\n"
        "Текущий режим: <b>🔥 Максимальный</b>\n\n"
        "• Multi-reason: <b>вкл</b>\n"
        "• Join → report inside: <b>вкл</b>\n"
        "• Негативные реакции: <b>вкл</b>\n"
        "• Жалобы на админов: <b>вкл</b>\n"
        "• Жалобы на связанные боты: <b>вкл</b>\n"
        "• Forward в @stopCA / @notoscam: <b>вкл</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── payment flow ──────────────────────────────────────────────────────────────

@router.callback_query(StrikeCb.filter(F.action == "buy"))
async def cb_strike_buy(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    # Уже есть доступ?
    if await _has_access(pool, callback.from_user.id):
        await callback.answer("⚔️ Strike уже активен!", show_alert=True)
        return

    wallet = _tron_wallet()
    ref = _gen_ref()
    for _ in range(5):
        existing = await pool.fetchrow("SELECT id FROM payments WHERE reference=$1", ref)
        if not existing:
            break
        ref = _gen_ref()

    await pool.execute(
        """INSERT INTO payments
               (user_id, plan, period_months, currency, amount_crypto, amount_usd,
                wallet_address, reference)
           VALUES ($1, 'strike', 0, 'USDT_TRC20', $2, $3, $4, $5)
           ON CONFLICT (reference) DO NOTHING""",
        callback.from_user.id, float(_PRICE_USD), float(_PRICE_USD),
        wallet or "NOT_CONFIGURED", ref,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить оплату", callback_data=StrikeCb(action="check_pay"))
    kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
    kb.adjust(1)

    if not wallet:
        await callback.message.edit_text(
            "⚔️ <b>Strike Module — $250 USDT</b>\n\n"
            "⚠️ Автоматическая оплата не настроена.\n\n"
            "Свяжитесь с администратором для активации.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
        return

    await callback.message.edit_text(
        f"⚔️ <b>Strike Module — оплата</b>\n\n"
        f"Сумма: <b>{_PRICE_USD} USDT</b>\n"
        f"Сеть: <b>TRC-20 (TRON)</b>\n\n"
        f"Кошелёк:\n<code>{wallet}</code>\n\n"
        f"Переведите ровно <b>{_PRICE_USD} USDT</b> и нажмите «Проверить оплату».\n"
        f"⚠️ Другие сети не принимаются.\n\n"
        f"⏱ Подтверждение: 5–30 минут\n"
        f"<i>ID платежа: {ref}</i>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(StrikeCb.filter(F.action == "check_pay"))
async def cb_strike_check_pay(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    if await _has_access(pool, callback.from_user.id):
        kb = InlineKeyboardBuilder()
        kb.button(text="⚔️ Открыть Strike", callback_data=StrikeCb(action="menu"))
        await callback.message.edit_text(
            "✅ <b>Strike Module активирован!</b>\n\n"
            "Доступ открыт. Добро пожаловать.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
        return

    row = await pool.fetchrow(
        "SELECT status, reference, created_at FROM payments "
        "WHERE user_id=$1 AND plan='strike' "
        "ORDER BY created_at DESC LIMIT 1",
        callback.from_user.id,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=StrikeCb(action="check_pay"))
    kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
    kb.adjust(1)

    if not row:
        await callback.message.edit_text(
            "❌ Платёж не найден. Создайте новый через «Купить».",
            reply_markup=kb.as_markup(),
        )
        return

    labels = {
        "pending":    "⏳ Ожидает оплаты",
        "confirming": "🔄 Подтверждается в блокчейне...",
        "confirmed":  "✅ Подтверждён — доступ активирован!",
        "expired":    "❌ Истёк",
    }
    await callback.message.edit_text(
        f"⚔️ <b>Статус платежа</b>\n\n"
        f"Статус: <b>{labels.get(row['status'], row['status'])}</b>\n"
        f"ID: <code>{row['reference']}</code>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── admin grant ───────────────────────────────────────────────────────────────

@router.callback_query(StrikeCb.filter(F.action == "admin_grant"))
async def cb_strike_admin_grant(
    callback: CallbackQuery, callback_data: StrikeCb, pool: asyncpg.Pool
) -> None:
    from bot.utils.subscription import is_platform_admin
    if not is_platform_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    target_id = callback_data.page  # page поле используется как target_user_id
    await pool.execute(
        "INSERT INTO strike_access (user_id, granted_by) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO NOTHING",
        target_id, callback.from_user.id,
    )
    await callback.answer(f"✅ Strike активирован для {target_id}", show_alert=True)
