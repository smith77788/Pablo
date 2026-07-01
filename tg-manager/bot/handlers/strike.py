"""Strike Module — платный модуль массовой зачистки нелегального контента.

Доступ: разовая оплата $250 USDT (TRC-20). Пожизненная лицензия.
Хранит доступ в таблице strike_access.
"""

from __future__ import annotations

import html
import logging
import os
import random
import string
import asyncio

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import StrikeCb, ChanCb, BmCb, AccCb
from bot.states import MiniStrikeFSM, StrikeEmailFSM
from bot.utils.subscription import require_feature
from bot.utils.event_status import mark_handled_error
from bot.utils.op_helpers import safe_answer, safe_edit
from services import email_oauth
from services.account_manager import (
    format_telegram_join_ref_display,
    normalize_telegram_join_ref,
)
from services.logger import log_exc_swallow

# SMTP авто-определение по домену почты
_SMTP_PRESETS: dict[str, tuple[str, int]] = {
    "gmail.com": ("smtp.gmail.com", 587),
    "googlemail.com": ("smtp.gmail.com", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 587),
    "yahoo.co.uk": ("smtp.mail.yahoo.com", 587),
    "yandex.ru": ("smtp.yandex.ru", 465),
    "yandex.com": ("smtp.yandex.ru", 465),
    "mail.ru": ("smtp.mail.ru", 465),
    "bk.ru": ("smtp.mail.ru", 465),
    "list.ru": ("smtp.mail.ru", 465),
    "icloud.com": ("smtp.mail.me.com", 587),
    "me.com": ("smtp.mail.me.com", 587),
    "protonmail.com": ("smtp.protonmail.com", 587),
    "proton.me": ("smtp.protonmail.com", 587),
}

_APP_PASSWORD_TIPS: dict[str, str] = {
    "gmail.com": (
        "Для Gmail нужен <b>пароль приложения</b>, не обычный пароль.\n"
        "Google Account → Безопасность → Двухэтапная верификация → "
        "Пароли приложений → Создать."
    ),
    "googlemail.com": (
        "Для Gmail нужен <b>пароль приложения</b>, не обычный пароль.\n"
        "Google Account → Безопасность → Двухэтапная верификация → "
        "Пароли приложений → Создать."
    ),
    "outlook.com": (
        "Для Outlook: включи двухфакторную аутентификацию, затем создай "
        "пароль приложения в настройках безопасности аккаунта Microsoft."
    ),
    "yandex.ru": (
        "Для Яндекс: Настройки → Безопасность → Пароли приложений → Создать новый."
    ),
    "mail.ru": (
        "Для Mail.ru: Настройки → Безопасность → Пароли для внешних приложений."
    ),
}

router = Router(name="strike")
log = logging.getLogger(__name__)

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
    granted_by   BIGINT,
    mode         TEXT DEFAULT 'normal'
);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='strike_access' AND column_name='mode'
    ) THEN
        ALTER TABLE strike_access ADD COLUMN mode TEXT DEFAULT 'normal';
    END IF;
END $$;
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
    try:
        await pool.execute(_CREATE_TABLE)
        _table_ok = True
    except Exception as e:
        log.warning("_ensure_table failed: %s", e)


async def _has_access(pool: asyncpg.Pool, user_id: int) -> bool:
    from bot.utils.subscription import is_platform_admin, get_plan

    if is_platform_admin(user_id):
        return True
    plan = await get_plan(pool, user_id)
    if plan == "enterprise":
        return True
    await _ensure_table(pool)
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM strike_access WHERE user_id=$1", user_id
        )
    except Exception:
        row = None
    result = row is not None
    if result:
        log.debug("strike access: user=%s has_access=%s", user_id, result)
    return result


def _menu_kb(has_access: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if has_access:
        kb.button(
            text="⚡ Мини-страйк (1 аккаунт)", callback_data=StrikeCb(action="mini")
        )
        kb.button(
            text="🚨 Одиночная цель", callback_data=ChanCb(action="br_mode_single")
        )
        kb.button(text="📋 Список целей", callback_data=ChanCb(action="br_mode_batch"))
        kb.button(text="⚙️ Настройки атаки", callback_data=StrikeCb(action="settings"))
        kb.button(text="📜 История", callback_data=StrikeCb(action="history"))
        kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
        kb.adjust(1, 2, 1, 1, 1)
    else:
        kb.button(text="💳 Купить за $250 USDT", callback_data=StrikeCb(action="buy"))
        kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
        kb.adjust(1, 1)
    return kb


# ── main menu ─────────────────────────────────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "menu"))
async def cb_strike_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    try:
        access = await _has_access(pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_strike_menu: _has_access failed")
        access = False

    if access:
        text = (
            "⚔️ <b>Strike Module</b> — активен\n\n"
            "<b>⚡ Мини-страйк</b> — 1 аккаунт, максимальный охват:\n"
            "• 12-векторная MTProto атака (все причины по кругу)\n"
            "• Email → abuse@telegram.org\n"
            "• Email → NCMEC CyberTipline (для CSAM)\n"
            "• Форма telegram.org/support\n\n"
            "<b>🚨 Одиночная / 📋 Список</b> — мульти-аккаунт страйк\n\n"
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
    try:
        access = await _has_access(pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_strike_settings: _has_access failed")
        access = False
    if not access:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await safe_answer(callback)

    try:
        row = await pool.fetchrow(
            "SELECT mode FROM strike_access WHERE user_id=$1", callback.from_user.id
        )
    except Exception:
        log_exc_swallow(log, "cb_strike_settings: fetchrow mode failed")
        row = None
    current_mode = row.get("mode", "normal") if row else "normal"

    mode_labels = {
        "fast": "⚡ Быстрый",
        "normal": "🔥 Нормальный",
        "maximum": "💀 Максимальный",
    }
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
        kb.button(
            text=f"{checked}{label}", callback_data=StrikeCb(action=f"set_mode_{m}")
        )
    kb.button(text="📧 Email аккаунты", callback_data=StrikeCb(action="emails"))
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


async def _set_strike_mode(
    callback: CallbackQuery, pool: asyncpg.Pool, mode: str
) -> None:
    try:
        await _ensure_table(pool)
        await pool.execute(
            """INSERT INTO strike_access (user_id, mode)
               VALUES ($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET mode = EXCLUDED.mode""",
            callback.from_user.id,
            mode,
        )
        log.info("strike mode set user=%s mode=%s", callback.from_user.id, mode)
    except Exception as exc:
        mark_handled_error(f"set_strike_mode: {exc}")
        log_exc_swallow(log, "set_strike_mode: DB failed")
        await callback.answer("Ошибка сохранения режима.", show_alert=True)
        return
    mode_labels = {
        "fast": "⚡ Быстрый",
        "normal": "🔥 Нормальный",
        "maximum": "💀 Максимальный",
    }
    # callback.answer() will be called by cb_strike_settings below;
    # calling it here first would cause a double-answer TelegramBadRequest.
    await cb_strike_settings(callback, pool)


@router.callback_query(StrikeCb.filter(F.action == "set_mode_fast"))
async def cb_strike_set_mode_fast(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await _set_strike_mode(callback, pool, "fast")


@router.callback_query(StrikeCb.filter(F.action == "set_mode_normal"))
async def cb_strike_set_mode_normal(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await _set_strike_mode(callback, pool, "normal")


@router.callback_query(StrikeCb.filter(F.action == "set_mode_maximum"))
async def cb_strike_set_mode_maximum(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await _set_strike_mode(callback, pool, "maximum")


# ── payment flow ──────────────────────────────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "buy"))
async def cb_strike_buy(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    # Уже есть доступ?
    try:
        _already = await _has_access(pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_strike_buy: _has_access failed")
        _already = False
    if _already:
        await callback.answer("⚔️ Strike уже активен!", show_alert=True)
        return
    await safe_answer(callback)

    wallet = _tron_wallet()
    ref = _gen_ref()
    try:
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
        log.info(
            "strike buy: payment record created user=%s ref=%s",
            callback.from_user.id,
            ref,
        )
    except Exception as exc:
        mark_handled_error(f"strike_buy: DB insert failed: {exc}")
        log_exc_swallow(log, "cb_strike_buy: DB insert failed")
        await callback.message.edit_text(
            "❌ Ошибка создания платежа. Попробуйте позже."
        )
        return

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
    await safe_answer(callback)

    try:
        has_acc = await _has_access(pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_strike_check_pay: _has_access failed")
        has_acc = False

    if has_acc:
        log.info("strike check_pay: already active user=%s", callback.from_user.id)
        kb = InlineKeyboardBuilder()
        kb.button(text="⚔️ Открыть Strike", callback_data=StrikeCb(action="menu"))
        await callback.message.edit_text(
            "✅ <b>Strike Module активирован!</b>\n\nДоступ открыт. Добро пожаловать.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    try:
        row = await pool.fetchrow(
            "SELECT status, reference, created_at FROM payments "
            "WHERE user_id=$1 AND plan='strike' "
            "ORDER BY created_at DESC LIMIT 1",
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "cb_strike_check_pay: fetchrow failed")
        row = None

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

    if row["status"] == "confirmed":
        await _ensure_table(pool)
        try:
            await pool.execute(
                "INSERT INTO strike_access (user_id, granted_by) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO NOTHING",
                callback.from_user.id,
                0,
            )
            log.info("strike_access granted via payment user=%s", callback.from_user.id)
        except Exception:
            log_exc_swallow(log, "cb_strike_check_pay: strike_access insert failed")
        kb_open = InlineKeyboardBuilder()
        kb_open.button(text="⚔️ Открыть Strike", callback_data=StrikeCb(action="menu"))
        await callback.message.edit_text(
            "✅ <b>Strike Module активирован!</b>\n\nДоступ открыт. Добро пожаловать.",
            parse_mode="HTML",
            reply_markup=kb_open.as_markup(),
        )
        return

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
    """Общий хелпер: показывает историю Strike.

    Раздел 1 — активные/ожидающие операции из operation_queue (тип strike).
    Раздел 2 — завершённые удары из strike_history (последние 10).
    """
    # ── Раздел 1: активные / ожидающие из operation_queue ────────────────────
    queue_rows: list = []
    try:
        queue_rows = await pool.fetch(
            """SELECT id, status, created_at, started_at, finished_at,
                      params->>'target' AS target,
                      params->>'reason' AS reason,
                      COALESCE(total_items, 0) AS total_items,
                      COALESCE(done_items, 0)  AS done_items,
                      error_msg
               FROM operation_queue
               WHERE owner_id=$1 AND op_type='strike'
               ORDER BY created_at DESC LIMIT 5""",
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "_show_strike_history: queue fetch failed")

    # ── Раздел 2: завершённые удары из strike_history ────────────────────────
    rows: list = []
    try:
        rows = await pool.fetch(
            """SELECT id, target, reason, preset, accounts_used, peer_reported, msgs_reported,
                      COALESCE(msgs_fetched, 0) AS msgs_fetched,
                      network_nodes, verified_down, duration_s, created_at
               FROM strike_history
               WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 10""",
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "_show_strike_history: history fetch failed")

    kb = InlineKeyboardBuilder()

    if not queue_rows and not rows:
        kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
        await callback.message.edit_text(
            "📜 <b>История атак</b>\n\nАтак ещё не было.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines: list[str] = ["📜 <b>История Strike</b>\n"]

    # ── Очередь ──────────────────────────────────────────────────────────────
    if queue_rows:
        _STATUS_ICONS = {
            "pending":  "⏳",
            "running":  "⚡",
            "done":     "✅",
            "failed":   "❌",
            "cancelled":"🚫",
        }
        lines.append("<b>Очередь операций:</b>")
        for q in queue_rows:
            icon = _STATUS_ICONS.get(q["status"], "❓")
            ts = q["created_at"].strftime("%d.%m %H:%M") if q["created_at"] else "?"
            tgt = _html.escape(q["target"] or "?")
            rsn = _html.escape(q["reason"] or "")
            progress = ""
            if q["status"] == "running" and q["total_items"] and q["total_items"] > 0:
                pct = int(q["done_items"] * 100 / q["total_items"])
                progress = f" {q['done_items']}/{q['total_items']} ({pct}%)"
            err_note = ""
            if q["status"] == "failed" and q["error_msg"]:
                err_note = f"\n   ⚠️ {_html.escape(str(q['error_msg'])[:80])}"
            lines.append(
                f"{icon} #{q['id']} <code>{tgt}</code> · {rsn} · {ts}{progress}{err_note}"
            )
        lines.append("")

    # ── Завершённые удары ─────────────────────────────────────────────────────
    rerun_rows: list = []
    seen_targets: set[str] = set()

    if rows:
        lines.append("<b>Выполненные удары (последние 10):</b>")
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

    kb.row(
        InlineKeyboardButton(
            text="◀️ Назад", callback_data=StrikeCb(action="menu").pack()
        )
    )

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
    await safe_answer(callback)
    await _show_strike_history(callback, pool)


# ── re-run: повтор удара по той же цели ──────────────────────────────────────


@router.callback_query(StrikeCb.filter(F.action == "rerun"))
async def cb_strike_rerun(
    callback: CallbackQuery,
    callback_data: StrikeCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    history_id = callback_data.page
    try:
        row = await pool.fetchrow(
            "SELECT target, reason, preset FROM strike_history WHERE id=$1 AND owner_id=$2",
            history_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "cb_strike_rerun: fetchrow failed")
        await callback.answer("Ошибка загрузки записи.", show_alert=True)
        return
    if not row:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await safe_answer(callback)
    log.info("strike rerun: user=%s target=%s", callback.from_user.id, row["target"])

    target = row["target"]
    reason = row["reason"]
    preset = row["preset"]

    # Pre-fill FSM state for the account picker (same flow as regular bulk report)
    from bot.states import BulkReportFSM
    await state.set_state(BulkReportFSM.selecting_accounts)
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
        _kb_na = InlineKeyboardBuilder()
        _kb_na.button(text="📱 Перейти к аккаунтам", callback_data=AccCb(action="menu"))
        _kb_na.button(text="◀️ История Strike", callback_data=StrikeCb(action="history"))
        _kb_na.button(text="⚔️ Меню Strike", callback_data=StrikeCb(action="menu"))
        _kb_na.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для повтора страйка нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт в разделе <b>📱 Аккаунты</b> и вернитесь сюда.",
            parse_mode="HTML",
            reply_markup=_kb_na.as_markup(),
        )
        return

    preset_info = f"пресет: <b>{preset}</b>" if preset else f"причина: <b>{reason}</b>"
    await _show_bulk_report_account_picker(
        callback.message,
        active,
        [],
        target,
        reason,
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
    try:
        await pool.execute(
            "INSERT INTO strike_access (user_id, granted_by) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO NOTHING",
            target_id,
            callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(f"❌ Ошибка: {exc}", show_alert=True)
        return
    await callback.answer(f"✅ Strike активирован для {target_id}", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
# MINI-STRIKE WIZARD
# Поток: /mini → ввод @channel → выбор категории → подтверждение → выполнение
# ══════════════════════════════════════════════════════════════════════════════


def _build_strike_intelligence_text(state_dict: dict, capacity_dict: dict) -> str:
    """Компактный intelligence-блок для экрана подтверждения STRIKE (макс. 8 строк)."""
    pressure = state_dict.get("pressure", {})
    accounts = state_dict.get("accounts", {})
    score = pressure.get("score", 0)
    emoji = pressure.get("emoji", "🟢")
    label = pressure.get("label", "Норма")
    available = accounts.get("available", 0)
    cooling = accounts.get("cooling", 0)
    total = accounts.get("total", 0)

    est_min = capacity_dict.get("estimated_minutes", 0)
    # Упрощённая вероятность успеха на основе давления и доступных аккаунтов
    success_pct = max(10, 100 - score) if available > 0 else 0

    lines = [
        "🎯 <b>Анализ операции STRIKE</b>",
        f"📊 Инфраструктура: {emoji} {label} ({score}/100)",
        f"👥 Аккаунты: ✅ {available}  ⏳ {cooling}  📱 {total}",
    ]
    if est_min and est_min > 0:
        lines.append(f"⚡ Прогноз: ⏱ ~{est_min:.0f} мин · 📈 {success_pct}%")
    else:
        lines.append(f"⚡ Вероятность успеха: 📈 {success_pct}%")

    # Рекомендации (максимум 2, только важные)
    recs = state_dict.get("recommendations", [])
    shown = 0
    for rec in recs:
        if shown >= 2:
            break
        severity = rec.get("severity", "")
        if severity in ("critical", "warning"):
            lines.append(f"⚠️ {rec.get('text', rec.get('message', ''))[:80]}")
            shown += 1

    return "\n".join(lines)


def _category_kb(target: str) -> InlineKeyboardBuilder:
    from services.strike_engine import MINI_CATEGORIES

    kb = InlineKeyboardBuilder()
    for key, cat in MINI_CATEGORIES.items():
        kb.button(
            text=cat["label"],
            callback_data=StrikeCb(action=f"mini_cat_{key}"),
        )
    kb.button(text="❌ Отмена", callback_data=StrikeCb(action="menu"))
    kb.adjust(1)
    return kb


@router.callback_query(StrikeCb.filter(F.action == "mini"))
async def cb_mini_strike_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await safe_answer(callback)
    await state.set_state(MiniStrikeFSM.awaiting_target)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=StrikeCb(action="menu"))
    await callback.message.edit_text(
        "⚡ <b>Мини-страйк</b>\n\n"
        "Введите username или ссылку на цель:\n"
        "• <code>@channelname</code>\n"
        "• <code>https://t.me/channelname</code>\n"
        "• <code>https://t.me/+HASH</code> — приватный канал по инвайт-ссылке\n"
        "• <code>https://t.me/joinchat/HASH</code> — старый формат инвайта\n\n"
        "Система бьёт по всем официальным каналам одновременно.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MiniStrikeFSM.awaiting_target)
async def msg_mini_strike_target(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await _has_access(pool, message.from_user.id):
        await state.clear()
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("⚠️ Введите username канала.", parse_mode="HTML")
        return

    # Нормализация — обрабатываем публичные username и приватные invite-ссылки (+HASH)
    ref_kind, ref_value = normalize_telegram_join_ref(raw)
    target = f"+{ref_value}" if ref_kind == "invite" else ref_value.lstrip("@")
    target_display = html.escape(format_telegram_join_ref_display(target))
    if not target or len(target) < 4:
        await message.answer("⚠️ Некорректный username или ссылка. Попробуйте ещё раз.")
        return

    await state.update_data(target=target)
    await state.set_state(MiniStrikeFSM.awaiting_category)

    await message.answer(
        f"🎯 Цель: <code>{target_display}</code>\n\nВыберите категорию нарушения:",
        parse_mode="HTML",
        reply_markup=_category_kb(target).as_markup(),
    )


@router.callback_query(StrikeCb.filter(F.action.startswith("mini_cat_")))
async def cb_mini_strike_category(
    callback: CallbackQuery,
    callback_data: StrikeCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    sd = await state.get_data()
    target = sd.get("target", "")

    category = callback_data.action.replace("mini_cat_", "")
    from services.strike_engine import MINI_CATEGORIES

    cat = MINI_CATEGORIES.get(category)
    if not cat:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return

    if not target:
        await callback.answer("Сессия истекла. Начните заново.", show_alert=True)
        await state.clear()
        return

    await safe_answer(callback)

    # Найти лучший активный аккаунт (учитывает кулдаун + risk score + infra_memory)
    from services import resource_selector

    try:
        acc = await resource_selector.select_account(
            pool, callback.from_user.id, action_type="strike"
        )
    except Exception:
        log_exc_swallow(log, "cb_mini_strike_category: select_account failed")
        acc = None

    if not acc:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=StrikeCb(action="menu"))
        await state.clear()
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Добавьте аккаунт в разделе <b>📱 Аккаунты</b>, затем вернитесь сюда.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    acc_label = acc.get("first_name") or acc.get("phone") or f"id{acc['id']}"
    trust = acc.get("trust_score") or 0

    await state.update_data(category=category, acc_id=acc["id"])

    # Проверяем наличие email-аккаунтов через БД (не глобальный SMTP)
    try:
        _email_count = await pool.fetchval(
            "SELECT COUNT(*) FROM strike_email_accounts WHERE owner_id=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
        smtp_status = (
            f"✅ настроен ({_email_count} ящик{'ов' if _email_count != 1 else ''})"
            if _email_count
            else "⚠️ не настроен (добавьте в ⚙️ Настройки → 📧 Email)"
        )
    except Exception:
        smtp_status = "⚠️ статус неизвестен"

    # Full pre-launch intelligence (intelligence_engine)
    intel_block = ""
    try:
        from services import intelligence_engine as _ie

        intel = await _ie.get_pre_launch_intelligence(
            pool, callback.from_user.id, "strike", 1
        )
        intel_block = _ie.format_pre_launch_block(intel)
    except Exception:
        # Fallback to simple state-based block
        try:
            from services import infra_orchestrator as _orch

            _intel_state, _intel_cap = await asyncio.gather(
                _orch.get_state(pool, callback.from_user.id),
                _orch.estimate_capacity(pool, callback.from_user.id, "strike", 1),
            )
            intel_block = _build_strike_intelligence_text(
                _intel_state.to_dict(), _intel_cap
            )
        except Exception:
            pass

    # Ecosystem context block
    eco_block = ""
    try:
        from services import ecosystem_brain as _eb

        _ecosystems = await _eb.list_ecosystems(pool, callback.from_user.id)
        if _ecosystems:
            _eco_lines = ["🌐 <b>Экосистемы:</b>"]
            for _eco in _ecosystems[:3]:
                _eco_health = await _eb.compute_health(
                    pool, _eco["id"], callback.from_user.id
                )
                _health_pct = int(_eco_health.overall * 100)
                _health_icon = (
                    "🟢"
                    if _eco_health.overall >= 0.7
                    else ("🟡" if _eco_health.overall >= 0.4 else "🔴")
                )
                _eco_lines.append(f"  {_health_icon} {_eco['name']}: {_health_pct}%")
            eco_block = "\n".join(_eco_lines)
    except Exception:
        pass

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить страйк", callback_data=StrikeCb(action="mini_run"))
    kb.button(text="❌ Отмена", callback_data=StrikeCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"⚡ <b>Мини-страйк — подтверждение</b>\n\n"
        f"🎯 Цель: <code>{html.escape(format_telegram_join_ref_display(str(target)))}</code>\n"
        f"📂 Категория: {cat['label']}\n"
        f"🔴 Уровень: <b>{cat['severity']}</b>\n"
        f"📱 Аккаунт: <b>{acc_label}</b> (trust: {trust:.2f})\n\n"
        f"<b>Будет выполнено:</b>\n"
        f"• Telethon MTProto — 12 векторов, 100+ сообщений, все причины\n"
        f"• Email abuse@telegram.org: {smtp_status}\n"
        f"{'• Email NCMEC CyberTipline: ' + smtp_status + chr(10) if cat.get('ncmec') else ''}"
        f"• Форма telegram.org/support\n\n"
        f"⏱ Ориентировочно: 2–5 минут"
        + (f"\n\n{intel_block}" if intel_block else "")
        + (f"\n\n{eco_block}" if eco_block else ""),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(StrikeCb.filter(F.action == "mini_run"))
async def cb_mini_strike_run(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    # Проверка давления инфраструктуры
    from services import infra_orchestrator

    ready, reason = await infra_orchestrator.is_ready_for_op(
        pool, callback.from_user.id
    )
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return
    warn = await infra_orchestrator.get_pressure_warning(pool, callback.from_user.id)
    await callback.answer(warn or "", show_alert=bool(warn))

    sd = await state.get_data()
    target = sd.get("target", "")
    category = sd.get("category", "fraud")
    acc_id = sd.get("acc_id")
    await state.clear()

    if not target or not acc_id:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await safe_edit(callback, "⚠️ Сессия истекла. Начните заново.", kb.as_markup())
        return

    # Загрузить аккаунт
    try:
        acc_row = await pool.fetchrow(
            """SELECT id, phone, first_name, session_str, trust_score,
                      device_model, system_version, app_version,
                      lang_code, system_lang_code, is_active,
                      (SELECT proxy_url FROM user_proxies up
                       WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url
               FROM tg_accounts WHERE id=$1 AND owner_id=$2""",
            acc_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "cb_mini_strike_run: acc fetchrow failed")
        acc_row = None

    if not acc_row or not acc_row["session_str"]:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await safe_edit(callback, "⚠️ Аккаунт не найден. Начните заново.", kb.as_markup())
        return

    acc = dict(acc_row)

    # ── Pre-flight health gate (Survival Contract §6): не запускать страйк на
    # неактивном/забаненном/остывающем/низкодоверенном аккаунте. ──
    from services import flood_engine as _fe

    if acc.get("is_active") is False:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await safe_edit(callback,
            "⛔️ Аккаунт неактивен (возможно, забанен). Выберите другой.",
            kb.as_markup())
        return
    try:
        _min_trust = _fe.min_trust_for_action("strike")
        _ts = float(acc.get("trust_score") or 0)
        if _ts < _min_trust:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from bot.callbacks import BmCb
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
            await safe_edit(callback,
                f"⛔️ Низкий trust_score аккаунта ({_ts:.2f} < {_min_trust:.2f}).\n"
                "Сначала прогрейте аккаунт через 🌱 Разогрев — иначе он улетит в бан.",
                kb.as_markup())
            return
    except (TypeError, ValueError):
        pass
    if _fe.is_account_cooling(acc["id"]):
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await safe_edit(callback,
            "⏳ Аккаунт на остывании после флуда/страйка. Попробуйте позже.",
            kb.as_markup())
        return

    # Claim the account so warmup/op_worker won't drive the same session in parallel.
    from services import op_worker as _opw

    if _opw.is_account_in_use(acc["id"]):
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await safe_edit(callback,
            "⏳ Аккаунт сейчас занят другой операцией. Попробуйте позже.",
            kb.as_markup())
        return
    await _opw.mark_accounts_in_use([acc["id"]])

    # Live-обновления в сообщение
    msg = callback.message
    last_text = [""]

    async def progress(text: str) -> None:
        full = f"⚡ <b>Страйк в процессе...</b>\n\n{text}"
        if full == last_text[0]:
            return
        last_text[0] = full
        try:
            await msg.edit_text(full, parse_mode="HTML")
        except Exception:
            pass

    await progress(
        f"🎯 Цель: <code>{html.escape(format_telegram_join_ref_display(str(target)))}</code>\n📂 Категория: {category}\n\n⚙️ Запуск..."
    )

    from services.strike_engine import execute_mini_strike, format_mini_result

    try:
        result = await execute_mini_strike(
            pool=pool,
            session_str=acc["session_str"],
            acc=acc,
            target=target,
            category=category,
            owner_id=callback.from_user.id,
            progress_cb=progress,
        )
    except Exception as e:
        mark_handled_error(f"mini_strike_run: {e}")
        log_exc_swallow(log, "cb_mini_strike_run: execute failed")
        _err_kb = InlineKeyboardBuilder()
        _err_kb.button(text="🔁 Попробовать снова", callback_data=StrikeCb(action="mini"))
        _err_kb.button(text="◀️ Меню Strike", callback_data=StrikeCb(action="menu"))
        _err_kb.adjust(1)
        await msg.edit_text(
            f"❌ <b>Ошибка выполнения страйка</b>\n\n<code>{str(e)[:200]}</code>",
            parse_mode="HTML",
            reply_markup=_err_kb.as_markup(),
        )
        return
    finally:
        # Всегда освобождаем claim аккаунта
        try:
            await _opw.release_accounts([acc["id"]])
        except Exception:
            log_exc_swallow(log, "cb_mini_strike_run: release_accounts failed")

    report_text = format_mini_result(result)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Ещё один страйк", callback_data=StrikeCb(action="mini"))
    kb.button(text="📜 История", callback_data=StrikeCb(action="history"))
    kb.button(text="◀️ Меню Strike", callback_data=StrikeCb(action="menu"))
    kb.adjust(1)

    await msg.edit_text(report_text, parse_mode="HTML", reply_markup=kb.as_markup())


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL ACCOUNT MANAGEMENT
# Добавление, просмотр, удаление почтовых ящиков для репортов
# ══════════════════════════════════════════════════════════════════════════════


async def _show_email_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Показать список email аккаунтов с кнопками управления."""
    try:
        rows = await pool.fetch(
            """SELECT id, email, smtp_host, smtp_port, is_active, fail_count, last_used_at,
                      COALESCE(auth_type, 'password') AS auth_type, oauth_provider
               FROM strike_email_accounts
               WHERE owner_id=$1
               ORDER BY added_at""",
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "_show_email_list: DB fetch failed")
        rows = []

    kb = InlineKeyboardBuilder()
    lines = ["📧 <b>Email аккаунты для репортов</b>\n"]

    if not rows:
        lines.append(
            "Пока нет ни одного email.\n\n"
            "Добавь Gmail, Outlook, Yandex или любой другой — "
            "система будет отправлять жалобы с каждого ящика."
        )
    else:
        lines.append(f"Добавлено: <b>{len(rows)}</b> ящиков\n")
        for r in rows:
            status = "✅" if r["is_active"] else "⛔"
            fails = f" · ошибок: {r['fail_count']}" if r["fail_count"] else ""
            lines.append(
                f"{status} <code>{r['email']}</code> ({r['smtp_host']}:{r['smtp_port']}){fails}"
            )
            # Кнопки: toggle + delete
            toggle_label = "⛔ Выключить" if r["is_active"] else "✅ Включить"
            kb.button(
                text=toggle_label,
                callback_data=StrikeCb(action="email_toggle", page=r["id"]),
            )
            kb.button(
                text=f"🗑 {r['email'][:20]}",
                callback_data=StrikeCb(action="email_del", page=r["id"]),
            )
        kb.adjust(2)

    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить email",
            callback_data=StrikeCb(action="email_add").pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="🔐 Gmail OAuth",
            callback_data=StrikeCb(action="email_oauth_google").pack(),
        ),
        InlineKeyboardButton(
            text="🔐 Outlook OAuth",
            callback_data=StrikeCb(action="email_oauth_microsoft").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="◀️ Настройки",
            callback_data=StrikeCb(action="settings").pack(),
        )
    )

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(StrikeCb.filter(F.action == "emails"))
async def cb_strike_emails(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await safe_answer(callback)
    await _show_email_list(callback, pool)


@router.callback_query(StrikeCb.filter(F.action == "email_toggle"))
async def cb_email_toggle(
    callback: CallbackQuery, callback_data: StrikeCb, pool: asyncpg.Pool
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    email_id = callback_data.page
    try:
        row = await pool.fetchrow(
            "SELECT is_active FROM strike_email_accounts WHERE id=$1 AND owner_id=$2",
            email_id,
            callback.from_user.id,
        )
        if not row:
            await callback.answer("Не найдено.", show_alert=True)
            return
        new_val = not row["is_active"]
        await pool.execute(
            "UPDATE strike_email_accounts SET is_active=$1, fail_count=0 WHERE id=$2",
            new_val,
            email_id,
        )
        await callback.answer("✅ Включён" if new_val else "⛔ Выключен")
    except Exception as exc:
        mark_handled_error(f"email_toggle: {exc}")
        log_exc_swallow(log, "cb_email_toggle: DB failed")
        await callback.answer("Ошибка.", show_alert=True)
        return
    await _show_email_list(callback, pool)


@router.callback_query(StrikeCb.filter(F.action == "email_del"))
async def cb_email_del(
    callback: CallbackQuery, callback_data: StrikeCb, pool: asyncpg.Pool
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    email_id = callback_data.page
    try:
        await pool.execute(
            "DELETE FROM strike_email_accounts WHERE id=$1 AND owner_id=$2",
            email_id,
            callback.from_user.id,
        )
        await callback.answer("🗑 Удалён")
    except Exception:
        log_exc_swallow(log, "cb_email_del: DB failed")
        await callback.answer("Ошибка.", show_alert=True)
        return
    await _show_email_list(callback, pool)


@router.callback_query(StrikeCb.filter(F.action == "email_oauth_google"))
@router.callback_query(StrikeCb.filter(F.action == "email_oauth_microsoft"))
async def cb_email_oauth(
    callback: CallbackQuery, callback_data: StrikeCb, pool: asyncpg.Pool
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if not await require_feature(pool, callback.from_user.id, "email_oauth"):
        await callback.answer(
            "OAuth почты доступен в MAX / Enterprise.", show_alert=True
        )
        return

    provider_name = (
        "google" if callback_data.action == "email_oauth_google" else "microsoft"
    )
    provider = email_oauth.PROVIDERS[provider_name]
    if not email_oauth.is_provider_configured(provider_name):
        missing = ", ".join(email_oauth.missing_provider_settings(provider_name))
        await callback.answer(
            f"{provider.label} OAuth не настроен. Нужно задать: {missing}",
            show_alert=True,
        )
        return

    url = email_oauth.build_auth_url(callback.from_user.id, provider_name)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔐 Подключить {provider.label}", url=url)
    kb.button(text="◀️ Email аккаунты", callback_data=StrikeCb(action="emails"))
    kb.adjust(1)
    await safe_answer(callback)
    await callback.message.edit_text(
        f"🔐 <b>{provider.label} OAuth</b>\n\n"
        "Нажмите кнопку ниже, войдите в почту и разрешите доступ для отправки SMTP. "
        "После успешной авторизации аккаунт появится в списке email.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(StrikeCb.filter(F.action == "email_add"))
async def cb_email_add(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await _has_access(pool, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await safe_answer(callback)
    await state.set_state(StrikeEmailFSM.awaiting_email)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=StrikeCb(action="emails"))
    await callback.message.edit_text(
        "📧 <b>Добавить email аккаунт</b>\n\n"
        "✏️ <b>Напишите ваш email-адрес в поле сообщения ниже ↓</b>\n\n"
        "Пример: <code>user@gmail.com</code>\n\n"
        "<b>Поддерживаются:</b> Gmail, Outlook, Яндекс, Mail.ru, Yahoo, iCloud, ProtonMail\n\n"
        "⚠️ Для Gmail и Outlook нужен <b>пароль приложения</b> "
        "(не обычный пароль) — после ввода email объясним как получить.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(StrikeEmailFSM.awaiting_email)
async def msg_email_input(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await _has_access(pool, message.from_user.id):
        await state.clear()
        return

    raw = (message.text or "").strip().lower()
    if "@" not in raw or "." not in raw.split("@")[-1]:
        await message.answer("⚠️ Некорректный email. Введите ещё раз.")
        return

    domain = raw.split("@")[-1]
    preset = _SMTP_PRESETS.get(domain)
    if preset:
        smtp_host, smtp_port = preset
        smtp_note = f"🔍 Определён автоматически: <b>{smtp_host}:{smtp_port}</b>"
    else:
        smtp_host, smtp_port = f"smtp.{domain}", 587
        smtp_note = (
            f"⚠️ Неизвестный провайдер. Попробуем: <b>{smtp_host}:{smtp_port}</b>"
        )

    tip = _APP_PASSWORD_TIPS.get(domain, "")

    await state.update_data(email=raw, smtp_host=smtp_host, smtp_port=smtp_port)
    await state.set_state(StrikeEmailFSM.awaiting_password)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=StrikeCb(action="emails"))

    tip_block = f"{tip}\n\n" if tip else ""
    await message.answer(
        f"📧 Email: <code>{raw}</code>\n"
        f"{smtp_note}\n\n"
        f"{tip_block}"
        "✏️ <b>Напишите пароль приложения в поле сообщения ниже ↓</b>\n\n"
        "<i>⚠️ Сообщение с паролем будет сразу удалено из чата.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(StrikeEmailFSM.awaiting_password)
async def msg_password_input(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    if not await _has_access(pool, message.from_user.id):
        await state.clear()
        return

    password = (message.text or "").strip()

    # Немедленно удаляем сообщение с паролем
    try:
        await message.delete()
    except Exception:
        pass

    if not password or len(password) < 4:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=StrikeCb(action="emails"))
        await message.answer(
            "⚠️ Слишком короткий пароль. Попробуйте ещё раз.",
            reply_markup=kb.as_markup(),
        )
        return

    sd = await state.get_data()
    email = sd.get("email", "")
    smtp_host = sd.get("smtp_host", "")
    smtp_port = sd.get("smtp_port", 587)
    await state.clear()

    if not email or not smtp_host:
        await message.answer("⚠️ Сессия истекла. Начните заново.")
        return

    # Тест подключения
    status_msg = await message.answer(
        f"🔄 Проверяю подключение к <b>{smtp_host}:{smtp_port}</b>...",
        parse_mode="HTML",
    )

    def _test_smtp() -> None:
        import smtplib
        import ssl as _ssl

        ctx = _ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=20) as srv:
                srv.login(email, password)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as srv:
                srv.ehlo()
                srv.starttls(context=ctx)
                srv.login(email, password)

    try:
        await asyncio.to_thread(_test_smtp)
        from services.token_vault import encrypt_token as _enc
        encrypted_pass = _enc(password)
        # Сохранить в БД (пароль зашифрован AES-256-GCM)
        await pool.execute(
            """INSERT INTO strike_email_accounts
               (owner_id, email, smtp_host, smtp_port, smtp_pass, auth_type)
               VALUES ($1, $2, $3, $4, $5, 'password')
               ON CONFLICT (owner_id, email)
               DO UPDATE SET smtp_host=$3, smtp_port=$4, smtp_pass=$5,
                             auth_type='password',
                             oauth_provider=NULL,
                             oauth_refresh_token=NULL,
                             oauth_access_token=NULL,
                             oauth_expires_at=NULL,
                             oauth_scopes='{}'::text[],
                              is_active=TRUE, fail_count=0""",
            message.from_user.id,
            email,
            smtp_host,
            smtp_port,
            encrypted_pass,
        )
        _kb_ok = InlineKeyboardBuilder()
        _kb_ok.button(
            text="➕ Добавить ещё", callback_data=StrikeCb(action="email_add")
        )
        _kb_ok.button(text="◀️ Список email", callback_data=StrikeCb(action="emails"))
        _kb_ok.button(text="⚔️ Меню Strike", callback_data=StrikeCb(action="menu"))
        _kb_ok.adjust(1)
        await status_msg.edit_text(
            f"✅ <b>Email добавлен: {email}</b>\n\n"
            f"Подключение к {smtp_host}:{smtp_port} — успешно\n\n"
            f"Теперь при каждом мини-страйке жалоба будет отправляться "
            f"с этого ящика на abuse@telegram.org.",
            parse_mode="HTML",
            reply_markup=_kb_ok.as_markup(),
        )
        log.info("strike: email added user=%s email=%s", message.from_user.id, email)
    except Exception as e:
        err = str(e)[:120]
        mark_handled_error(f"email_add smtp_test: {err}")
        log.warning("strike: email test failed %s: %s", email, err)
        kb = InlineKeyboardBuilder()
        kb.button(
            text="🔁 Попробовать снова", callback_data=StrikeCb(action="email_add")
        )
        kb.button(text="◀️ Список email", callback_data=StrikeCb(action="emails"))
        kb.adjust(1)
        await status_msg.edit_text(
            f"❌ <b>Не удалось подключиться</b>\n\n"
            f"Email: <code>{email}</code>\n"
            f"SMTP: {smtp_host}:{smtp_port}\n"
            f"Ошибка: <code>{err}</code>\n\n"
            f"<b>Возможные причины:</b>\n"
            f"• Неверный пароль приложения\n"
            f"• Для Gmail/Outlook нужен именно пароль приложения, не обычный\n"
            f"• SMTP заблокирован провайдером\n"
            f"• Двухфакторная аутентификация не включена",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
