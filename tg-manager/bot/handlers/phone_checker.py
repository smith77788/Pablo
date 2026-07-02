"""Чекер номеров телефонов.

Проверяет список номеров на регистрацию в Telegram.
Результат: статистика + CSV с данными (id, username, имя, премиум).
"""
from __future__ import annotations

import html
import io
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import PhoneCheckerCb, BmCb

log = logging.getLogger(__name__)
router = Router()


class PhoneCheckerFSM(StatesGroup):
    phones = State()


# ── Утилиты ──────────────────────────────────────────────────────────────────

async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=PhoneCheckerCb(action="menu"))
    return kb.as_markup()


async def _get_best_account(pool: asyncpg.Pool, owner_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT id, session_str, api_id, api_hash, device_model, system_version, "
        "app_version, lang_code, system_lang_code, proxy_url "
        "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "AND session_str IS NOT NULL AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        owner_id,
    )


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.callback_query(PhoneCheckerCb.filter(F.action == "menu"))
async def cb_phone_checker_menu(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Проверить номера", callback_data=PhoneCheckerCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)
    await _edit(
        callback,
        "📱 <b>Чекер номеров телефонов</b>\n\n"
        "Проверяет, зарегистрированы ли номера в Telegram.\n\n"
        "Для каждого найденного номера определяет:\n"
        "• Telegram ID пользователя\n"
        "• @username (если публичный)\n"
        "• Имя и фамилию\n"
        "• Наличие Telegram Premium\n\n"
        "Результат — CSV файл со всеми данными.",
        kb.as_markup(),
    )


# ── Ввод номеров ─────────────────────────────────────────────────────────────

@router.callback_query(PhoneCheckerCb.filter(F.action == "start"))
async def cb_phone_checker_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PhoneCheckerFSM.phones)
    await _edit(
        callback,
        "📱 <b>Введите номера телефонов</b>\n\n"
        "Каждый номер с новой строки или через запятую:\n\n"
        "<code>+79991234567\n+7 800 555 35 35\n89991234567</code>\n\n"
        "Максимум 5000 номеров за раз.",
        _cancel_kb(),
    )


@router.message(PhoneCheckerFSM.phones)
async def msg_phone_checker_phones(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services.phone_checker_engine import parse_phone_list

    phones = parse_phone_list(message.text or "")
    if not phones:
        await message.answer("⚠️ Не удалось распознать номера. Введите в формате +79991234567")
        return

    await state.clear()

    # Ищем лучший аккаунт для проверки
    acc = await _get_best_account(pool, message.from_user.id)
    if not acc:
        await message.answer("⚠️ Нет доступных аккаунтов. Добавьте аккаунты в раздел Аккаунты.")
        return

    prog_msg = await message.answer(
        f"⏳ Проверяю <b>{len(phones)}</b> номеров...\n"
        "Это может занять несколько минут.",
        parse_mode="HTML",
    )

    # Запускаем проверку батчами
    from services.phone_checker_engine import check_phones_batch, results_to_csv, _BATCH_SIZE

    all_results = []
    for i in range(0, len(phones), _BATCH_SIZE):
        batch = phones[i:i + _BATCH_SIZE]
        try:
            batch_results = await check_phones_batch(acc["session_str"], dict(acc), batch)
            all_results.extend(batch_results)
        except Exception as exc:
            log.warning("phone_checker: batch %d error: %s", i, exc)
        if i + _BATCH_SIZE < len(phones):
            import asyncio
            await asyncio.sleep(2.0)

    # Статистика
    registered = [r for r in all_results if r.get("registered") is True]
    not_registered = [r for r in all_results if r.get("registered") is False]
    with_premium = [r for r in registered if r.get("premium")]
    with_username = [r for r in registered if r.get("username")]

    # CSV
    csv_bytes = results_to_csv(all_results)
    csv_file = BufferedInputFile(csv_bytes, filename=f"phone_check_{len(phones)}.csv")

    summary = (
        f"✅ <b>Проверка завершена</b>\n\n"
        f"📱 Всего номеров: <b>{len(phones)}</b>\n"
        f"✅ Зарегистрированы в TG: <b>{len(registered)}</b>\n"
        f"❌ Не зарегистрированы: <b>{len(not_registered)}</b>\n"
        f"⭐ С Premium: <b>{len(with_premium)}</b>\n"
        f"👤 С @username: <b>{len(with_username)}</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Проверить ещё", callback_data=PhoneCheckerCb(action="start"))
    kb.button(text="◀️ В меню", callback_data=PhoneCheckerCb(action="menu"))
    kb.adjust(2)

    try:
        await prog_msg.delete()
    except Exception:
        pass

    await message.answer_document(
        csv_file,
        caption=summary,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
