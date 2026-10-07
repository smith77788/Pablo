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
_table_ok = False

_DISCLAIMER = (
    "\n\n<i>⚠️ <b>Важно:</b> Strike Module является инструментом для подачи "
    "законных жалоб через официальные механизмы Telegram Trust &amp; Safety. "
    "Результат зависит исключительно от решения модераторов Telegram. "
    "Использование модуля не гарантирует удаление или блокировку ресурса.</i>"
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS strike_access (
    user_id      BIGINT PRIMARY KEY,
    purchased_at TIMESTAMPTZ DEFAULT now(),
    payment_ref  TEXT,
    granted_by   BIGINT
)
"""


# ── helpers ──────────────────────────────────────────────────────────────────


def _tron_wallet() -> str:
    return os.getenv("TRON_WALLET", "")


def _gen_ref() -> str:
    return "STK-" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=10)
    )


async def _ensure_table(pool: asyncpg.Pool) -> None:
    global _table_ok
    if _table_ok:
        return
    await pool.execute(_CREATE_TABLE)
    _table_ok = True


async def _has_access(pool: asyncpg.Pool, user_id: int) -> bool:
    from bot.utils.subscription import is_platform_admin, get_plan

    if is_platform_admin(user_id):
        return True
    plan = await get_plan(pool, user_id)
    if plan == "enterprise":
        return True
    await _ensure_table(pool)
    row = await pool.fetchrow("SELECT 1 FROM strike_access WHERE user_id=$1", user_id)
    return row is not None


def _menu_kb(has_access: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if has_access:
        kb.button(
            text="🚨 Одиночная цель", callback_data=ChanCb(action="br_mode_single")
        )
        kb.button(text="📋 Список целей", callback_data=ChanCb(action="br_mode_batch"))
        kb.button(text="⚙️ Настройки атаки", callback_data=StrikeCb(action="settings"))
        kb.button(text="📜 История атак",    callback_data=StrikeCb(action="history"))
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        kb.adjust(2, 1, 1, 1)
    else:
        kb.button(text="💳 Купить за $250 USDT", callback_data=StrikeCb(action="buy"))
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        kb.adjust(1, 1)
    return kb


# ── main menu ─────────────────────────────────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "menu"))
async def cb_strike_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    access = await _has_access(pool, callback.from_user.id)

    if access:
        text = (
            "⚔️ <b>Strike Module</b> — активен\n\n"
            "<b>12-векторная атака на нелегальный ресурс:</b>\n"
            "① Жалоба на ресурс — все доступные причины\n"
            "② Жалоба на фото профиля канала\n"
            "③ Вход в канал → жалобы изнутри\n"
            "④ Жалобы на закреплённые сообщения\n"
            "⑤ Жалобы на 50 последних сообщений\n"
            "⑥ Спам-сигнал channels.ReportSpam\n"
            "⑦ Реакции 👎💩 на все доступные посты\n"
            "⑧ Жалобы на ВСЕХ администраторов\n"
            "⑨ Жалоба на связанную группу обсуждений\n"
            "⑩ Жалобы на связанные боты\n"
            "⑪ Пересылка доказательств в @stopCA / @notoscam\n"
            "⑫ Заглушить + заблокировать + выйти\n\n"
            "Выберите режим:" + _DISCLAIMER
        )
    else:
        text = (
            "⚔️ <b>Strike Module</b>\n\n"
            "<b>Модуль массовой зачистки нелегального контента</b>\n\n"
            "12-векторная скоординированная атака с нескольких аккаунтов против:\n"
            "• 🟣 Наркотики и запрещённые вещества\n"
            "• 💣 Терроризм и экстремизм\n"
            "• 🚨 CSAM (детский контент)\n"
            "• 🕸 Даркнет-услуги\n"
            "• 🔫 Торговля оружием\n"
            "• 💸 Мошенничество\n\n"
            "<b>Каждый аккаунт выполняет 12 действий:</b> жалобы с "
            "разными причинами на канал, фото, сообщения, закреплённые посты, "
            "администраторов, связанные группы и боты, пересылка "
            "в Telegram Trust &amp; Safety.\n\n"
            "💰 <b>Стоимость:</b> $250 USDT · Пожизненный доступ · "
            "Неограниченное использование" + _DISCLAIMER
        )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_menu_kb(access).as_markup(),
    )


# ── settings stub ─────────────────────────────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "settings"))
async def cb_strike_settings(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    access = await _has_access(pool, callback.from_user.id)
    if not access:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()

    row = await pool.fetchrow("SELECT mode FROM strike_access WHERE user_id=$1", callback.from_user.id)
    current_mode = row.get("mode", "normal") if row else "normal"

    mode_labels = {"fast": "⚡ Быстрый", "normal": "🔥 Нормальный", "maximum": "💀 Максимальный"}
    mode_desc = {
        "fast": "6 векторов · быстро · безопаснее для аккаунтов",
        "normal": "12 векторов · стандартный баланс",
        "maximum": "12 векторов + расширенное давление · максимальная интенсивность",
    }
    current_label = mode_labels.get(current_mode, "🔥 Нормальный")
    current_desc = mode_desc.get(current_mode, "")

    kb = InlineKeyboardBuilder()
    for m, label in mode_labels.items():
        checked = "✅ " if m == current_mode else ""
        kb.button(text=f"{checked}{label}", callback_data=StrikeCb(action=f"set_mode_{m}"))
    kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"⚙️ <b>Настройки Strike — Режим</b>\n\n"
        f"Текущий режим: <b>{current_label}</b>\n"
        f"<i>{current_desc}</i>\n\n"
        f"<b>⚡ Быстрый</b> — 6 векторов: ReportPeer + ReportPhoto + ReportPinned + ReportMessages. "
        f"Не входит в канал, не реагирует, быстрее выполняется.\n\n"
        f"<b>🔥 Нормальный</b> — 12 векторов по умолчанию: все виды жалоб, вход в канал, "
        f"реакции 👎, жалобы на администраторов, пересылка в @SpamBot.\n\n"
        f"<b>💀 Максимальный</b> — 12 векторов + усиленное давление: до 100 сообщений, "
        f"все связанные ресурсы, максимальная интенсивность." + _DISCLAIMER,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _set_strike_mode(callback: CallbackQuery, pool: asyncpg.Pool, mode: str) -> None:
    await _ensure_table(pool)
    # Upsert — works for purchased users AND enterprise/admin (no row in strike_access)
    await pool.execute(
        """INSERT INTO strike_access (user_id, mode)
           VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE SET mode = EXCLUDED.mode""",
        callback.from_user.id, mode,
    )
    mode_labels = {"fast": "⚡ Быстрый", "normal": "🔥 Нормальный", "maximum": "💀 Максимальный"}
    await callback.answer(f"✅ Режим: {mode_labels.get(mode, mode)}", show_alert=True)
    await cb_strike_settings(callback, pool)


@router.callback_query(StrikeCb.filter(F.action == "set_mode_fast"))
async def cb_strike_set_mode_fast(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await _set_strike_mode(callback, pool, "fast")


@router.callback_query(StrikeCb.filter(F.action == "set_mode_normal"))
async def cb_strike_set_mode_normal(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await _set_strike_mode(callback, pool, "normal")


@router.callback_query(StrikeCb.filter(F.action == "set_mode_maximum"))
async def cb_strike_set_mode_maximum(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await _set_strike_mode(callback, pool, "maximum")


# ── payment flow ──────────────────────────────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "buy"))
async def cb_strike_buy(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    # Уже есть доступ?
    if await _has_access(pool, callback.from_user.id):
        await callback.answer("⚔️ Strike уже активен!", show_alert=True)
        return
    await callback.answer()

    wallet = _tron_wallet()
    ref = _gen_ref()
    for _ in range(5):
        existing = await pool.fetchrow(
            "SELECT id FROM payments WHERE reference=$1", ref
        )
        if not existing:
            break
        ref = _gen_ref()

    await pool.execute(
        """INSERT INTO payments
               (user_id, plan, period_months, currency, amount_crypto, amount_usd,
                wallet_address, reference)
           VALUES ($1, 'strike', 0, 'USDT_TRC20', $2, $3, $4, $5)
           ON CONFLICT (reference) DO NOTHING""",
        callback.from_user.id,
        float(_PRICE_USD),
        float(_PRICE_USD),
        wallet or "NOT_CONFIGURED",
        ref,
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
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
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
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(StrikeCb.filter(F.action == "check_pay"))
async def cb_strike_check_pay(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    if await _has_access(pool, callback.from_user.id):
        kb = InlineKeyboardBuilder()
        kb.button(text="⚔️ Открыть Strike", callback_data=StrikeCb(action="menu"))
        await callback.message.edit_text(
            "✅ <b>Strike Module активирован!</b>\n\nДоступ открыт. Добро пожаловать.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
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
        "pending": "⏳ Ожидает оплаты",
        "confirming": "🔄 Подтверждается в блокчейне...",
        "confirmed": "✅ Подтверждён — доступ активирован!",
        "expired": "❌ Истёк",
    }
    await callback.message.edit_text(
        f"⚔️ <b>Статус платежа</b>\n\n"
        f"Статус: <b>{labels.get(row['status'], row['status'])}</b>\n"
        f"ID: <code>{row['reference']}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── strike history ────────────────────────────────────────────────────────────

import html as _html


async def _show_strike_history(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Общий хелпер: показывает историю Strike."""
    rows = await pool.fetch(
        """SELECT id, target, reason, preset, accounts_used, peer_reported, msgs_reported,
                  COALESCE(msgs_fetched, 0) AS msgs_fetched,
                  network_nodes, verified_down, duration_s, created_at
           FROM strike_history
           WHERE owner_id=$1
           ORDER BY created_at DESC LIMIT 10""",
        callback.from_user.id,
    )

    kb = InlineKeyboardBuilder()

    if not rows:
        kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
        await callback.message.edit_text(
            "📜 <b>История атак</b>\n\nАтак ещё не было.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines = ["📜 <b>История Strike (последние 10)</b>\n"]
    rerun_rows = []  # rows eligible for re-run buttons (unique targets, max 5)
    seen_targets: set[str] = set()
    for r in rows:
        # 🟢 = подтверждено удаление, ⚔️ = полный удар, 🟡 = только ReportPeer, 🔴 = не прошёл
        _pr = r["peer_reported"] or 0
        _mr = r["msgs_reported"] or 0
        _mf = r["msgs_fetched"] or 0
        if r["verified_down"]:
            status = "🟢"
        elif _pr > 0 and (_mr > 0 or _mf > 0):
            status = "⚔️"
        elif _pr > 0:
            status = "🟡"
        else:
            status = "🔴"
        ts = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "?"
        msgs_r = r["msgs_reported"] or 0
        msgs_f = r["msgs_fetched"] or 0
        msgs_str = f"{msgs_r}/{msgs_f}" if msgs_f > msgs_r else str(msgs_r)
        preset_label = f" [{r['preset']}]" if r["preset"] else ""
        lines.append(
            f"{status} <code>{_html.escape(r['target'])}</code> · {ts}\n"
            f"   {r['reason']}{preset_label} · {r['accounts_used']} акк · "
            f"{r['peer_reported']} жалоб · сообщ: {msgs_str} · {int(r['duration_s'] or 0)}с"
        )
        # Collect distinct targets for re-run buttons (first occurrence wins)
        if r["target"] not in seen_targets and len(rerun_rows) < 5:
            seen_targets.add(r["target"])
            rerun_rows.append(r)

    # Re-run buttons: one per unique target (max 5), compact label
    if rerun_rows:
        lines.append("\n<i>Нажмите 🔁 чтобы повторить удар по той же цели:</i>")
        for r in rerun_rows:
            short = r["target"][:20]
            kb.button(
                text=f"🔁 {short}",
                callback_data=StrikeCb(action="rerun", page=r["id"]),
            )
        kb.adjust(2)

    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=StrikeCb(action="menu").pack()))

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(StrikeCb.filter(F.action == "history"))
async def cb_strike_history(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await _show_strike_history(callback, pool)


# Обработчик кнопки "История" из финального отчёта (callback_data="strike:history")
@router.callback_query(F.data == "strike:history")
async def cb_strike_history_shortcut(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await _show_strike_history(callback, pool)


# ── re-run: повтор удара по той же цели ──────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "rerun"))
async def cb_strike_rerun(
    callback: CallbackQuery, callback_data: StrikeCb,
    pool: asyncpg.Pool, state: FSMContext,
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    history_id = callback_data.page
    row = await pool.fetchrow(
        "SELECT target, reason, preset FROM strike_history WHERE id=$1 AND owner_id=$2",
        history_id, callback.from_user.id,
    )
    if not row:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await callback.answer()

    target = row["target"]
    reason = row["reason"]
    preset = row["preset"]

    # Pre-fill FSM state for the account picker (same flow as regular bulk report)
    await state.update_data(
        peer=target,
        peers=[target],
        reason=reason,
        preset=preset,
        selected_ids=[],
    )

    from bot.handlers.channel_ops import _show_bulk_report_account_picker, _get_accounts
    accounts = await _get_accounts(pool, callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов. Добавьте или активируйте аккаунты.",
            parse_mode="HTML",
        )
        return

    preset_info = f"пресет: <b>{preset}</b>" if preset else f"причина: <b>{reason}</b>"
    await _show_bulk_report_account_picker(
        callback.message, active, [], target, reason,
        edit=True,
        extra_info=f"🔁 Повтор Strike · {preset_info}",
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
    await _ensure_table(pool)
    await pool.execute(
        "INSERT INTO strike_access (user_id, granted_by) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO NOTHING",
        target_id,
        callback.from_user.id,
    )
    await callback.answer(f"✅ Strike активирован для {target_id}", show_alert=True)
