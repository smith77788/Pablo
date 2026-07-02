"""Сеттер профилей — массовое оформление аккаунтов.

Операции:
  • Установить имя / фамилию / bio (со спинтаксом)
  • Установить аватар (из URL)
  • Установить 2FA пароль
  • Применить ко всем или выбранным аккаунтам
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ProfileSetterCb, BmCb

log = logging.getLogger(__name__)
router = Router()


class SetterFSM(StatesGroup):
    value = State()     # получаем значение от пользователя
    acc_count = State() # кол-во аккаунтов


# ── Утилиты ──────────────────────────────────────────────────────────────────

async def _edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await cb.answer()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ProfileSetterCb(action="menu"))
    return kb.as_markup()


async def _total_accs(pool: asyncpg.Pool, owner_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
        owner_id,
    )
    return int(row["cnt"]) if row else 0


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "menu"))
async def cb_setter_menu(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await state.clear()
    total = await _total_accs(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Имя / Фамилия / Bio", callback_data=ProfileSetterCb(action="set_name"))
    kb.button(text="🖼 Аватар (URL)", callback_data=ProfileSetterCb(action="set_avatar"))
    kb.button(text="🔑 2FA пароль", callback_data=ProfileSetterCb(action="set_2fa"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)
    await _edit(
        callback,
        "🎨 <b>Сеттер профилей</b>\n\n"
        "Массовое оформление аккаунтов: имя, bio, аватар, 2FA.\n"
        "Поддерживает спинтакс для рандомизации: <code>{Привет|Hi|Hola}</code>\n\n"
        f"🔑 Доступно аккаунтов: <b>{total}</b>",
        kb.as_markup(),
    )


# ── Имя / Bio ─────────────────────────────────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "set_name"))
async def cb_set_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(op="name")
    await state.set_state(SetterFSM.value)
    await _edit(
        callback,
        "📝 <b>Имя / Фамилия / Bio</b>\n\n"
        "Введите данные в формате (каждое поле с новой строки):\n\n"
        "<code>Имя: {Алекс|Макс|Игорь}\n"
        "Фамилия: {Петров|Иванов}\n"
        "Bio: {Предприниматель|Бизнес|Услуги}</code>\n\n"
        "Пустое поле = не менять. Спинтакс {A|B} — рандомный выбор для каждого акк.",
        _cancel_kb(),
    )


@router.message(SetterFSM.value, F.text)
async def msg_setter_value(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    op = data.get("op", "")
    text = message.text or ""

    if op == "name":
        parsed = _parse_name_bio(text)
        if not any(parsed.values()):
            await message.answer("⚠️ Не распознано. Формат:\nИмя: Текст\nФамилия: Текст\nBio: Текст")
            return
        await state.update_data(name_data=parsed)
    elif op == "avatar":
        url = text.strip()
        if not url.startswith("http"):
            await message.answer("⚠️ Введите прямую ссылку на изображение (https://...)")
            return
        await state.update_data(avatar_url=url)
    elif op == "2fa":
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        new_pass = parts[0] if parts else ""
        current_pass = parts[1] if len(parts) > 1 else ""
        hint = parts[2] if len(parts) > 2 else ""
        if not new_pass or len(new_pass) < 4:
            await message.answer("⚠️ Пароль должен быть минимум 4 символа")
            return
        await state.update_data(new_password=new_pass, current_password=current_pass, hint=hint)

    total = await _total_accs(pool, message.from_user.id)
    await state.set_state(SetterFSM.acc_count)
    await message.answer(
        f"✅ Данные приняты.\n\n"
        f"Доступно аккаунтов: <b>{total}</b>\n"
        "Сколько аккаунтов оформить? (0 = все):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(SetterFSM.acc_count)
async def msg_setter_acc_count(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int(message.text or "0")
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    owner_id = message.from_user.id
    total = await _total_accs(pool, owner_id)
    use = min(n, total) if n > 0 else total
    if use == 0:
        await message.answer("⚠️ Нет доступных аккаунтов.")
        return
    await state.update_data(acc_count=use)
    data = await state.get_data()
    op = data.get("op", "")

    op_labels = {"name": "Имя/Bio", "avatar": "Аватар", "2fa": "2FA пароль"}
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Применить", callback_data=ProfileSetterCb(action="confirm"))
    kb.button(text="❌ Отмена", callback_data=ProfileSetterCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        f"🎨 <b>Сеттер — подтверждение</b>\n\n"
        f"Операция: <b>{op_labels.get(op, op)}</b>\n"
        f"🔑 Аккаунтов: <b>{use}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Аватар ────────────────────────────────────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "set_avatar"))
async def cb_set_avatar(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(op="avatar")
    await state.set_state(SetterFSM.value)
    await _edit(
        callback,
        "🖼 <b>Установить аватар</b>\n\n"
        "Введите прямую ссылку на изображение (JPG/PNG):\n\n"
        "<code>https://example.com/photo.jpg</code>",
        _cancel_kb(),
    )


# ── 2FA ───────────────────────────────────────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "set_2fa"))
async def cb_set_2fa(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(op="2fa")
    await state.set_state(SetterFSM.value)
    await _edit(
        callback,
        "🔑 <b>Установить 2FA пароль</b>\n\n"
        "Введите данные (каждое с новой строки):\n\n"
        "<code>НовыйПароль\nТекущийПароль (если уже стоит)\nПодсказка (необязательно)</code>\n\n"
        "<i>Если 2FA ещё не установлен — строку текущего пароля оставьте пустой.</i>",
        _cancel_kb(),
    )


# ── Подтверждение ─────────────────────────────────────────────────────────────

@router.callback_query(ProfileSetterCb.filter(F.action == "confirm"))
async def cb_setter_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.clear()
    owner_id = callback.from_user.id
    acc_count = data.get("acc_count", 1)
    op = data.get("op", "")

    rows = await pool.fetch(
        "SELECT id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
        owner_id, acc_count,
    )
    account_ids = [r["id"] for r in rows]
    if not account_ids:
        await callback.answer("⚠️ Нет доступных аккаунтов", show_alert=True)
        return

    import json
    params: dict = {"op": op, "account_ids": account_ids}
    if op == "name":
        params["name_data"] = data.get("name_data", {})
    elif op == "avatar":
        params["avatar_url"] = data.get("avatar_url", "")
    elif op == "2fa":
        params["new_password"] = data.get("new_password", "")
        params["current_password"] = data.get("current_password", "")
        params["hint"] = data.get("hint", "")

    label_map = {"name": "Имя/Bio", "avatar": "Аватар", "2fa": "2FA пароль"}
    label = f"Сеттер: {label_map.get(op, op)} × {len(account_ids)} акк."
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
        "VALUES($1,'bulk_set_profile','pending',$2,$3,$4) RETURNING id",
        owner_id, json.dumps(params), len(account_ids), label,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="◀️ В меню", callback_data=ProfileSetterCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        f"✅ <b>Сеттер поставлен в очередь</b>\n\n"
        f"🆔 Операция: <b>#{op_id}</b>\n"
        f"{html.escape(label)}",
        kb.as_markup(),
    )


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _parse_name_bio(text: str) -> dict:
    result = {"first_name": "", "last_name": "", "about": ""}
    for line in text.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("имя:"):
            result["first_name"] = line[4:].strip()
        elif low.startswith("фамилия:"):
            result["last_name"] = line[8:].strip()
        elif low.startswith("bio:") or low.startswith("о себе:") or low.startswith("about:"):
            result["about"] = line.split(":", 1)[1].strip()
        elif not any(result.values()):
            result["first_name"] = line  # первая строка = имя
    return result
