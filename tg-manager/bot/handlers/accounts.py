"""Telegram personal account manager handler.

Users connect their own Telegram accounts (phone + OTP + optional 2FA via Telethon)
so the platform can list channels/groups, post messages, and track search rankings.
"""
from __future__ import annotations

import re
from html import escape

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

_DIALOGS_PAGE_SIZE = 10

from bot.callbacks import AccCb, BotCb
from bot.keyboards import subscription_locked_markup
from bot.utils.subscription import get_plan, locked_text
from config import TG_API_ID, TG_API_HASH
from database import db
from services.account_manager import (
    check_account_health,
    cleanup_pending,
    confirm_2fa,
    confirm_code,
    get_account_dialogs_stats,
    get_dialogs,
    get_client_info_and_session,
    send_message,
    send_message_via_account,
    start_login,
)

router = Router()

# ── Plan limits ────────────────────────────────────────────────────────────────

ACC_LIMITS: dict[str, int] = {
    "free": 0,
    "starter": 1,
    "pro": 3,
    "enterprise": 9999,
}

# ── FSM States ─────────────────────────────────────────────────────────────────


class AccountLogin(StatesGroup):
    waiting_phone = State()
    waiting_code = State()   # state data: phone, phone_code_hash
    waiting_2fa = State()    # state data: phone


class AccountPost(StatesGroup):
    choosing_chat = State()   # unused in handler body; present for context
    waiting_text = State()    # state data: acc_id, chat_id


class AccountSendMsg(StatesGroup):
    waiting_chat_id = State()  # state data: acc_id
    waiting_text = State()     # state data: acc_id, chat_id


# ── Helpers ────────────────────────────────────────────────────────────────────

def _api_configured() -> bool:
    """Return True if Telethon credentials are set in config."""
    try:
        return bool(TG_API_ID and TG_API_HASH)
    except Exception:
        return False


def _api_missing_text() -> str:
    return (
        "⚙️ <b>API не настроен</b>\n\n"
        "Для работы с личными аккаунтами необходимо задать переменные окружения:\n"
        "<code>TG_API_ID</code> и <code>TG_API_HASH</code>\n\n"
        "Получить их можно на <a href=\"https://my.telegram.org/apps\">my.telegram.org/apps</a>.\n"
        "После добавления — перезапустите бота."
    )


async def _get_account_limit(pool: asyncpg.Pool, user_id: int) -> tuple[str, int]:
    """Return (plan, limit) for this user."""
    plan = await get_plan(pool, user_id)
    return plan, ACC_LIMITS.get(plan, 0)


def _acc_menu_markup(acc_id: int, is_active: bool = True):
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Каналы/группы",
              callback_data=AccCb(action="channels", acc_id=acc_id))
    kb.button(text="📤 Написать",
              callback_data=AccCb(action="post", acc_id=acc_id))
    kb.button(text="🔍 Проверить",
              callback_data=AccCb(action="check_health", acc_id=acc_id))
    kb.button(text="📊 Диалоги",
              callback_data=AccCb(action="dialogs_stats", acc_id=acc_id))
    kb.button(text="📂 Список диалогов",
              callback_data=AccCb(action="dialogs", acc_id=acc_id, chat_id=0))
    kb.button(text="✉️ Отправить",
              callback_data=AccCb(action="send_msg", acc_id=acc_id))
    toggle_text = "⏸ Отключить" if is_active else "▶️ Включить"
    kb.button(text=toggle_text,
              callback_data=AccCb(action="toggle", acc_id=acc_id))
    kb.button(text="🗑 Удалить",
              callback_data=AccCb(action="remove", acc_id=acc_id))
    kb.button(text="◀️ Мои аккаунты",
              callback_data=AccCb(action="menu"))
    kb.adjust(2, 2, 1, 1, 2, 1)
    return kb.as_markup()


def _cancel_markup():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена",
              callback_data=AccCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


# ── /cancel — выход из любого FSM-состояния ───────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(
            "ℹ️ Нет активного действия для отмены.",
            reply_markup=_cancel_markup(),
        )
        return
    await state.clear()
    await message.answer(
        "❌ <b>Действие отменено.</b>\n\nВоспользуйтесь /accounts для управления аккаунтами.",
        parse_mode="HTML",
    )


# ── /accounts command ──────────────────────────────────────────────────────────

@router.message(Command("accounts"))
async def cmd_accounts(message: Message, pool: asyncpg.Pool) -> None:
    await _show_accounts_menu(message, pool, message.from_user.id, edit=False)


@router.callback_query(AccCb.filter(F.action == "menu"))
async def cb_accounts_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    await _show_accounts_menu(callback.message, pool, callback.from_user.id, edit=True)


async def _show_accounts_menu(
    message: Message,
    pool: asyncpg.Pool,
    user_id: int,
    *,
    edit: bool,
) -> None:
    plan, limit = await _get_account_limit(pool, user_id)

    if limit == 0:
        text = locked_text("Личные аккаунты Telegram", "starter")
        markup = subscription_locked_markup("starter")
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    accounts = await db.get_tg_accounts(pool, user_id)
    kb = InlineKeyboardBuilder()

    if accounts:
        lines = ["👤 <b>Подключённые аккаунты:</b>\n"]
        for acc in accounts:
            name = escape(acc["first_name"] or "")
            uname = f"@{escape(acc['username'])}" if acc.get("username") else ""
            phone = escape(acc.get("phone", ""))
            label = name or uname or phone or f"ID {acc['id']}"
            display = f"{label} ({phone})" if phone and name else label
            lines.append(f"  • {display}")
            kb.button(
                text=f"👤 {display}",
                callback_data=AccCb(action="view", acc_id=acc["id"]),
            )
        text = "\n".join(lines)
    else:
        text = "👤 <b>Личные аккаунты</b>\n\nУ вас нет подключённых аккаунтов."

    kb.adjust(1)

    limit_label = "∞" if limit >= 9999 else str(limit)
    used = len(accounts) if accounts else 0
    text += f"\n\n<i>Использовано: {used} / {limit_label}</i>"

    if used < limit:
        kb.button(text="➕ Добавить аккаунт",
                  callback_data=AccCb(action="add"))

    kb.button(text="◀️ Главное меню",
              callback_data=BotCb(action="list", page=0))
    kb.adjust(1)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Add account ────────────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "add"))
async def cb_add_account(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()

    if not _api_configured():
        await callback.message.edit_text(
            _api_missing_text(),
            parse_mode="HTML",
            reply_markup=_cancel_markup(),
        )
        return

    plan, limit = await _get_account_limit(pool, callback.from_user.id)
    if limit == 0:
        await callback.message.edit_text(
            locked_text("Личные аккаунты Telegram", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return

    accounts = await db.get_tg_accounts(pool, callback.from_user.id)
    if len(accounts) >= limit:
        limit_label = "∞" if limit >= 9999 else str(limit)
        await callback.message.edit_text(
            f"⚠️ Достигнут лимит аккаунтов для вашего плана "
            f"(<b>{plan.upper()}</b>: {limit_label} аккаунт{'ов' if limit != 1 else ''}).\n\n"
            f"Обновите подписку, чтобы добавить больше аккаунтов.",
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(plan),
        )
        return

    await state.set_state(AccountLogin.waiting_phone)
    await callback.message.edit_text(
        "📱 <b>Добавление аккаунта</b>\n\n"
        "Введите номер телефона в международном формате:\n"
        "<code>+79001234567</code>",
        parse_mode="HTML",
        reply_markup=_cancel_markup(),
    )


# ── Step 1: receive phone ──────────────────────────────────────────────────────

@router.message(AccountLogin.waiting_phone)
async def handle_phone(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    phone = (message.text or "").strip()

    if not re.match(r"^\+\d{7,15}$", phone):
        await message.answer(
            "❌ Неверный формат номера.\n"
            "Введите номер в формате: <code>+79001234567</code>",
            parse_mode="HTML",
        )
        return

    await message.answer("⏳ Отправляю код на " + escape(phone) + "…")

    try:
        phone_code_hash = await start_login(phone)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await message.answer(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"❌ Ошибка при отправке кода: <code>{escape(err[:200])}</code>",
                parse_mode="HTML",
            )
        return

    await state.update_data(phone=phone, phone_code_hash=phone_code_hash)
    await state.set_state(AccountLogin.waiting_code)
    await message.answer(
        f"✅ Код отправлен на <code>{escape(phone)}</code>.\n\n"
        f"Введите его (только цифры, например <code>12345</code>):",
        parse_mode="HTML",
        reply_markup=_cancel_markup(),
    )


# ── Step 2: receive OTP code ───────────────────────────────────────────────────

@router.message(AccountLogin.waiting_code)
async def handle_code(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    code = (message.text or "").strip()
    data = await state.get_data()
    phone: str = data.get("phone", "")
    phone_code_hash: str = data.get("phone_code_hash", "")

    if not code.isdigit():
        await message.answer("❌ Код должен содержать только цифры. Введите ещё раз:")
        return

    try:
        result = await confirm_code(phone, code, phone_code_hash)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await message.answer(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
            )
        elif "SessionPasswordNeededError" in err or "need_2fa" in err.lower():
            await state.set_state(AccountLogin.waiting_2fa)
            await message.answer(
                "🔐 Аккаунт защищён двухфакторной аутентификацией.\n\n"
                "Введите ваш пароль 2FA:",
                parse_mode="HTML",
                reply_markup=_cancel_markup(),
            )
        else:
            await message.answer(
                f"❌ Ошибка подтверждения кода: <code>{escape(err[:200])}</code>",
                parse_mode="HTML",
            )
        return

    # confirm_code may signal "need_2fa" as a dict/string result instead of exception
    if result == "need_2fa" or (isinstance(result, dict) and result.get("need_2fa")):
        await state.set_state(AccountLogin.waiting_2fa)
        await message.answer(
            "🔐 Аккаунт защищён двухфакторной аутентификацией.\n\n"
            "Введите ваш пароль 2FA:",
            parse_mode="HTML",
            reply_markup=_cancel_markup(),
        )
        return

    await _finalize_login(message, pool, state, phone)


# ── Step 3: receive 2FA password ───────────────────────────────────────────────

@router.message(AccountLogin.waiting_2fa)
async def handle_2fa(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    password = (message.text or "").strip()
    data = await state.get_data()
    phone: str = data.get("phone", "")

    try:
        await confirm_2fa(phone, password)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await message.answer(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
            )
            return
        if "PasswordHashInvalidError" in err or "invalid" in err.lower():
            await message.answer(
                "❌ Неверный пароль 2FA. Попробуйте снова:"
            )
            return
        await message.answer(
            f"❌ Ошибка 2FA: <code>{escape(err[:200])}</code>",
            parse_mode="HTML",
        )
        return

    await _finalize_login(message, pool, state, phone)


# ── Login finalization ─────────────────────────────────────────────────────────

async def _finalize_login(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    phone: str,
) -> None:
    """Fetch session, save to DB, clean up pending login state."""
    try:
        session_str, info = await get_client_info_and_session(phone)
    except Exception as exc:
        await message.answer(
            f"❌ Не удалось получить сессию: <code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        await state.clear()
        return

    try:
        await db.add_tg_account(
            pool,
            owner_id=message.from_user.id,
            phone=info.get("phone") or phone,
            session_str=session_str,
            tg_user_id=info.get("tg_user_id"),
            first_name=info.get("first_name", ""),
            username=info.get("username", ""),
        )
    except Exception as exc:
        await message.answer(
            f"❌ Не удалось сохранить аккаунт: <code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        await state.clear()
        return

    await cleanup_pending(phone)
    await state.clear()

    display_name = escape(info.get("first_name") or info.get("username") or phone)
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        f"✅ <b>Аккаунт успешно добавлен!</b>\n\n"
        f"👤 {display_name}\n"
        f"📱 {escape(phone)}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── View account ───────────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "view"))
async def cb_view_account(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    name = escape(acc.get("first_name") or "")
    uname = f"@{escape(acc['username'])}" if acc.get("username") else ""
    phone = escape(acc.get("phone") or "")
    tg_id = acc.get("tg_user_id") or ""
    is_active = bool(acc.get("is_active", True))

    lines = ["👤 <b>Аккаунт</b>\n"]
    if name:
        lines.append(f"Имя: <b>{name}</b>")
    if uname:
        lines.append(f"Username: <b>{uname}</b>")
    if phone:
        lines.append(f"Телефон: <code>{phone}</code>")
    if tg_id:
        lines.append(f"Telegram ID: <code>{tg_id}</code>")
    lines.append(f"Статус: {'✅ Активен' if is_active else '⏸ Отключён'}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_acc_menu_markup(callback_data.acc_id, is_active=is_active),
    )


# ── Channels / groups list ─────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "channels"))
async def cb_channels(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text(
        "⏳ Загружаю список каналов и групп…",
        parse_mode="HTML",
    )

    try:
        dialogs = await get_dialogs(session_str)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await callback.message.edit_text(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        else:
            await callback.message.edit_text(
                f"❌ Не удалось получить список диалогов:\n"
                f"<code>{escape(err[:200])}</code>",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        return

    if not dialogs:
        await callback.message.edit_text(
            "📭 Каналов и групп не найдено.",
            parse_mode="HTML",
            reply_markup=_acc_menu_markup(callback_data.acc_id),
        )
        return

    kb = InlineKeyboardBuilder()
    lines = ["📋 <b>Каналы и группы:</b>\n"]
    for dialog in dialogs[:30]:
        title = escape(dialog.get("title") or "Без названия")
        members = dialog.get("members") or dialog.get("participants_count") or 0
        chat_id = dialog.get("id") or 0
        lines.append(f"  • {title}" + (f" — {members:,} участн." if members else ""))
        kb.button(
            text=f"📤 {title[:28]}",
            callback_data=AccCb(
                action="post_to",
                acc_id=callback_data.acc_id,
                chat_id=chat_id,
            ),
        )

    kb.button(text="◀️ Назад", callback_data=AccCb(action="view", acc_id=callback_data.acc_id))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Post: choose chat ──────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "post"))
async def cb_post_choose_chat(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text("⏳ Загружаю список каналов…", parse_mode="HTML")

    try:
        dialogs = await get_dialogs(session_str)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await callback.message.edit_text(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        else:
            await callback.message.edit_text(
                f"❌ Не удалось загрузить каналы:\n<code>{escape(err[:200])}</code>",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        return

    if not dialogs:
        await callback.message.edit_text(
            "📭 Нет доступных каналов/групп для отправки.",
            parse_mode="HTML",
            reply_markup=_acc_menu_markup(callback_data.acc_id),
        )
        return

    kb = InlineKeyboardBuilder()
    for dialog in dialogs[:30]:
        title = dialog.get("title") or "Без названия"
        chat_id = dialog.get("id") or 0
        kb.button(
            text=f"📣 {title[:30]}",
            callback_data=AccCb(
                action="post_to",
                acc_id=callback_data.acc_id,
                chat_id=chat_id,
            ),
        )
    kb.button(text="◀️ Назад", callback_data=AccCb(action="view", acc_id=callback_data.acc_id))
    kb.adjust(1)

    await callback.message.edit_text(
        "📤 <b>Выберите канал или группу для отправки сообщения:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Post: set destination → ask for text ──────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "post_to"))
async def cb_post_to(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    await state.set_state(AccountPost.waiting_text)
    await state.update_data(acc_id=callback_data.acc_id, chat_id=callback_data.chat_id)

    await callback.message.edit_text(
        "✏️ <b>Введите текст сообщения</b>, которое нужно опубликовать:",
        parse_mode="HTML",
        reply_markup=_cancel_markup(),
    )


# ── Post: receive message text and send ───────────────────────────────────────

@router.message(AccountPost.waiting_text)
async def handle_post_text(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Сообщение не может быть пустым. Введите текст:")
        return

    data = await state.get_data()
    acc_id: int = data.get("acc_id", 0)
    chat_id: int = data.get("chat_id", 0)

    if not acc_id or not chat_id:
        await message.answer("❌ Ошибка: не выбран аккаунт или канал. Начните заново.")
        await state.clear()
        return

    acc = await db.get_tg_account(pool, acc_id, message.from_user.id)
    if not acc:
        await message.answer("❌ Аккаунт не найден.")
        await state.clear()
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await message.answer("⏳ Отправляю сообщение…")

    try:
        await send_message(session_str, chat_id, text)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await message.answer(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"❌ Не удалось отправить сообщение:\n<code>{escape(err[:200])}</code>",
                parse_mode="HTML",
            )
        await state.clear()
        return

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Аккаунт", callback_data=AccCb(action="view", acc_id=acc_id))
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        "✅ <b>Сообщение успешно отправлено!</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Remove account ─────────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "remove"))
async def cb_remove_account(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    name = escape(acc.get("first_name") or "")
    phone = escape(acc.get("phone") or "")
    label = name or phone or f"ID {callback_data.acc_id}"

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=AccCb(action="remove_confirm", acc_id=callback_data.acc_id),
    )
    kb.button(
        text="❌ Отмена",
        callback_data=AccCb(action="view", acc_id=callback_data.acc_id),
    )
    kb.adjust(2)

    await callback.message.edit_text(
        f"🗑 <b>Удалить аккаунт?</b>\n\n"
        f"👤 {label}\n\n"
        f"<i>Сессия будет удалена из системы. Восстановить без повторного входа нельзя.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AccCb.filter(F.action == "remove_confirm"))
async def cb_remove_confirm(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await db.remove_tg_account(pool, callback_data.acc_id, callback.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "✅ <b>Аккаунт удалён.</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Check account health ───────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "check_health"))
async def cb_check_health(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text(
        "🔍 <b>Проверяю аккаунт…</b>",
        parse_mode="HTML",
    )

    try:
        result = await check_account_health(session_str)
    except Exception as exc:
        result = {"ok": False, "reason": f"Ошибка: {escape(str(exc)[:200])}"}

    if result["ok"]:
        status_icon = "✅"
        status_title = "Аккаунт в порядке"
    else:
        status_icon = "❌"
        status_title = "Проблема с аккаунтом"

    reason = escape(result.get("reason", ""))
    await callback.message.edit_text(
        f"{status_icon} <b>{status_title}</b>\n\n"
        f"{reason}",
        parse_mode="HTML",
        reply_markup=_acc_menu_markup(callback_data.acc_id),
    )


# ── Dialogs stats ──────────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "dialogs_stats"))
async def cb_dialogs_stats(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text(
        "📊 <b>Загружаю статистику диалогов…</b>",
        parse_mode="HTML",
    )

    try:
        stats = await get_account_dialogs_stats(session_str)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await callback.message.edit_text(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        else:
            await callback.message.edit_text(
                f"❌ Не удалось получить статистику:\n<code>{escape(err[:200])}</code>",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        return

    await callback.message.edit_text(
        f"📊 <b>Статистика диалогов</b>\n\n"
        f"📁 Всего диалогов: <b>{stats.get('total', 0)}</b>\n"
        f"📢 Каналы: <b>{stats.get('channels', 0)}</b>\n"
        f"👥 Группы: <b>{stats.get('groups', 0)}</b>\n"
        f"💬 Личные чаты: <b>{stats.get('personal', 0)}</b>",
        parse_mode="HTML",
        reply_markup=_acc_menu_markup(callback_data.acc_id),
    )


# ── Dialogs list with pagination ───────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "dialogs"))
async def cb_dialogs(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""
    page_offset = callback_data.chat_id  # используем chat_id как page offset

    await callback.message.edit_text(
        "⏳ <b>Загружаю список диалогов…</b>",
        parse_mode="HTML",
    )

    # Загружаем на одну страницу больше, чтобы проверить наличие следующей
    fetch_limit = _DIALOGS_PAGE_SIZE + 1
    try:
        dialogs = await get_dialogs(session_str, limit=fetch_limit + page_offset, offset=0)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await callback.message.edit_text(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        else:
            await callback.message.edit_text(
                f"❌ Не удалось получить диалоги:\n<code>{escape(err[:200])}</code>",
                parse_mode="HTML",
                reply_markup=_acc_menu_markup(callback_data.acc_id),
            )
        return

    # Срезаем уже просмотренные страницы
    page_dialogs = dialogs[page_offset:]
    has_next = len(page_dialogs) > _DIALOGS_PAGE_SIZE
    page_dialogs = page_dialogs[:_DIALOGS_PAGE_SIZE]

    if not page_dialogs:
        await callback.message.edit_text(
            "📭 Диалогов не найдено.",
            parse_mode="HTML",
            reply_markup=_acc_menu_markup(callback_data.acc_id),
        )
        return

    _type_labels = {"channel": "📢 Канал", "group": "👥 Группа"}
    lines = [f"📂 <b>Диалоги (стр. {page_offset // _DIALOGS_PAGE_SIZE + 1})</b>\n"]
    for dialog in page_dialogs:
        title = escape(dialog.get("title") or "Без названия")
        d_type = _type_labels.get(dialog.get("type", ""), "💬")
        members = dialog.get("members") or 0
        members_str = f" · {members:,} уч." if members else ""
        lines.append(f"  • {d_type} {title}{members_str}")

    kb = InlineKeyboardBuilder()

    # Навигация
    next_offset = page_offset + _DIALOGS_PAGE_SIZE
    prev_offset = page_offset - _DIALOGS_PAGE_SIZE

    nav_row = []
    if page_offset > 0:
        kb.button(
            text="◀️ Назад",
            callback_data=AccCb(action="dialogs", acc_id=callback_data.acc_id, chat_id=max(0, prev_offset)),
        )
    if has_next:
        kb.button(
            text="▶️ Далее",
            callback_data=AccCb(action="dialogs", acc_id=callback_data.acc_id, chat_id=next_offset),
        )
    kb.button(
        text="◀️ К аккаунту",
        callback_data=AccCb(action="view", acc_id=callback_data.acc_id),
    )

    nav_count = (1 if page_offset > 0 else 0) + (1 if has_next else 0)
    if nav_count == 2:
        kb.adjust(2, 1)
    else:
        kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Toggle account active status ───────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "toggle"))
async def cb_toggle_account(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    current_status = bool(acc.get("is_active", True))
    new_status = not current_status

    updated = await db.update_tg_account_status(
        pool, callback_data.acc_id, callback.from_user.id, new_status
    )
    if not updated:
        await callback.answer("Не удалось обновить статус.", show_alert=True)
        return

    status_text = "▶️ <b>Аккаунт включён.</b>" if new_status else "⏸ <b>Аккаунт отключён.</b>"
    name = escape(acc.get("first_name") or "")
    uname = f"@{escape(acc['username'])}" if acc.get("username") else ""
    phone = escape(acc.get("phone") or "")
    tg_id = acc.get("tg_user_id") or ""

    lines = [f"👤 <b>Аккаунт</b>\n", status_text]
    if name:
        lines.append(f"Имя: <b>{name}</b>")
    if uname:
        lines.append(f"Username: <b>{uname}</b>")
    if phone:
        lines.append(f"Телефон: <code>{phone}</code>")
    if tg_id:
        lines.append(f"Telegram ID: <code>{tg_id}</code>")
    lines.append(f"Статус: {'✅ Активен' if new_status else '⏸ Отключён'}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_acc_menu_markup(callback_data.acc_id, is_active=new_status),
    )


# ── Send message via personal account (FSM) ────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "send_msg"))
async def cb_send_msg_start(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    await state.set_state(AccountSendMsg.waiting_chat_id)
    await state.update_data(acc_id=callback_data.acc_id)

    await callback.message.edit_text(
        "✉️ <b>Отправка сообщения из личного аккаунта</b>\n\n"
        "⚠️ <i>Отправка из вашего личного аккаунта — будьте осторожны с частотой.</i>\n\n"
        "Введите <b>chat_id</b> или <b>@username</b> получателя:\n"
        "<code>@channel_name</code> или <code>123456789</code>",
        parse_mode="HTML",
        reply_markup=_cancel_markup(),
    )


@router.message(AccountSendMsg.waiting_chat_id)
async def handle_send_msg_chat_id(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ Введите chat_id или @username:")
        return

    # Принимаем @username или числовой chat_id
    if raw.startswith("@"):
        chat_id_value: str | int = raw
    elif raw.lstrip("-").isdigit():
        chat_id_value = int(raw)
    else:
        await message.answer(
            "❌ Неверный формат. Введите числовой ID или @username:"
        )
        return

    await state.update_data(chat_id=chat_id_value)
    await state.set_state(AccountSendMsg.waiting_text)

    await message.answer(
        f"✏️ Получатель: <code>{escape(str(chat_id_value))}</code>\n\n"
        "Введите <b>текст сообщения</b>:",
        parse_mode="HTML",
        reply_markup=_cancel_markup(),
    )


@router.message(AccountSendMsg.waiting_text)
async def handle_send_msg_text(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Сообщение не может быть пустым. Введите текст:")
        return

    data = await state.get_data()
    acc_id: int = data.get("acc_id", 0)
    chat_id = data.get("chat_id")

    if not acc_id or chat_id is None:
        await message.answer("❌ Ошибка состояния. Начните заново.")
        await state.clear()
        return

    acc = await db.get_tg_account(pool, acc_id, message.from_user.id)
    if not acc:
        await message.answer("❌ Аккаунт не найден.")
        await state.clear()
        return

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await message.answer("⏳ Отправляю сообщение…")

    ok = await send_message_via_account(session_str, chat_id, text)

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Аккаунт", callback_data=AccCb(action="view", acc_id=acc_id))
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    if ok:
        await message.answer(
            "✅ <b>Сообщение успешно отправлено!</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer(
            "❌ <b>Не удалось отправить сообщение.</b>\n\n"
            "<i>Проверьте корректность chat_id/@username и убедитесь, "
            "что аккаунт имеет доступ к этому чату.</i>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
