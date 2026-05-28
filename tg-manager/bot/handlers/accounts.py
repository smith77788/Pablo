"""Telegram personal account manager handler.

Users connect their own Telegram accounts (phone + OTP + optional 2FA via Telethon)
so the platform can list channels/groups, post messages, and track search rankings.
"""
from __future__ import annotations

import asyncio
import re
from html import escape

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

_DIALOGS_PAGE_SIZE = 10

from bot.callbacks import AccCb, BotCb, ChanCb
from bot.keyboards import subscription_locked_markup
from bot.utils.subscription import get_plan, locked_text
from config import TG_API_ID, TG_API_HASH
from database import db
from services.account_manager import (
    check_account_health,
    cleanup_pending,
    cleanup_qr_pending,
    confirm_2fa,
    confirm_code,
    confirm_qr_2fa,
    generate_device_fingerprint,
    get_account_dialogs_stats,
    get_dialogs,
    get_client_info_and_session,
    import_from_pyrogram_json,
    import_from_session_string,
    resend_code as resend_login_code,
    import_from_tdata,
    send_message,
    send_message_via_account,
    start_login,
    start_qr_login,
    wait_qr_login,
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


class SessionImport(StatesGroup):
    waiting_string_session = State()
    waiting_pyrogram_json = State()
    waiting_tdata_zip = State()


class AccountPost(StatesGroup):
    choosing_chat = State()   # unused in handler body; present for context
    waiting_text = State()    # state data: acc_id, chat_id


class AccountSendMsg(StatesGroup):
    waiting_chat_id = State()  # state data: acc_id
    waiting_text = State()     # state data: acc_id, chat_id


class QrLogin2FA(StatesGroup):
    waiting_password = State()  # state data: user_id (implicit from FSM context)


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
    kb.button(text="🌐 Прокси",
              callback_data=AccCb(action="set_proxy", acc_id=acc_id))
    toggle_text = "⏸ Отключить" if is_active else "▶️ Включить"
    kb.button(text=toggle_text,
              callback_data=AccCb(action="toggle", acc_id=acc_id))
    kb.button(text="🗑 Удалить",
              callback_data=AccCb(action="remove", acc_id=acc_id))
    kb.button(text="◀️ Мои аккаунты",
              callback_data=AccCb(action="menu"))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def _cancel_markup():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена",
              callback_data=AccCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


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
        lines = [
            "📱 <b>Личные Telegram-аккаунты</b>\n",
            "Аккаунты используются для создания каналов/групп, "
            "вступления, публикаций и других операций в Telegram.\n",
            "👤 <b>Подключённые аккаунты:</b>\n",
        ]
        for acc in accounts:
            name = escape(acc["first_name"] or "")
            uname = f"@{escape(acc['username'])}" if acc.get("username") else ""
            phone = escape(acc.get("phone", ""))
            label = name or uname or phone or f"ID {acc['id']}"
            display = f"{label} ({phone})" if phone and name else label
            status = "✅" if acc.get("is_active") else "⛔"
            lines.append(f"  {status} {display}")
            kb.button(
                text=f"{status} {display}",
                callback_data=AccCb(action="view", acc_id=acc["id"]),
            )
        text = "\n".join(lines)
    else:
        text = (
            "📱 <b>Личные Telegram-аккаунты</b>\n\n"
            "Здесь подключаются личные аккаунты Telegram (не боты).\n"
            "Они нужны для:\n"
            "• Создания каналов и групп\n"
            "• Вступления/выхода из каналов\n"
            "• Публикации постов от имени аккаунта\n"
            "• Создания ботов через @BotFather\n\n"
            "Добавьте первый аккаунт ↓"
        )

    kb.adjust(1)

    limit_label = "∞" if limit >= 9999 else str(limit)
    used = len(accounts) if accounts else 0
    text += f"\n\n<i>Использовано: {used} / {limit_label}</i>"

    if used < limit:
        kb.button(text="🔲 Добавить (QR-код)",
                  callback_data=AccCb(action="qr_login"))
        kb.button(text="☎️ Добавить (номер телефона)",
                  callback_data=AccCb(action="add"))
        kb.button(text="📥 Импорт сессии",
                  callback_data=AccCb(action="import_menu"))

    kb.button(text="📡 Операции с аккаунтами", callback_data=ChanCb(action="menu"))
    kb.button(text="⚡ Массовые операции",     callback_data=ChanCb(action="bulk_menu"))
    kb.button(text="◀️ Главное меню",
              callback_data=BotCb(action="main"))
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
        phone_code_hash, delivery_hint = await start_login(phone)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            m = re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await message.answer(
                f"⏳ Слишком много запросов. Попробуйте через <b>{wait} сек</b>.",
                parse_mode="HTML",
            )
        elif "TG_API_ID" in err or "TG_API_HASH" in err:
            await message.answer(
                "⚙️ <b>API-ключи не настроены.</b>\n\n"
                "Обратитесь к администратору платформы — "
                "необходимо задать переменные <code>TG_API_ID</code> и <code>TG_API_HASH</code>.",
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

    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Выслать SMS", callback_data=AccCb(action="resend_sms"))
    kb.button(text="❌ Отмена",      callback_data=AccCb(action="cancel_login"))
    kb.adjust(1)

    await message.answer(
        f"✅ {delivery_hint} <code>{escape(phone)}</code>.\n\n"
        f"Код обычно приходит как уведомление <b>в приложении Telegram</b> "
        f"(не SMS) — проверьте на всех устройствах.\n\n"
        f"Не пришёл? Нажмите <b>«Выслать SMS»</b>.\n\n"
        f"Введите код (только цифры, например <code>12345</code>):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Resend code via SMS ────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "resend_sms"))
async def cb_resend_sms(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    phone: str = data.get("phone", "")
    phone_code_hash: str = data.get("phone_code_hash", "")

    if not phone or not phone_code_hash:
        await callback.message.answer("❌ Сессия истекла. Начните заново: /accounts")
        await state.clear()
        return

    try:
        new_hash, hint = await resend_login_code(phone, phone_code_hash)
    except Exception as exc:
        err = str(exc)
        if "FloodWait" in type(exc).__name__ or "flood" in err.lower():
            import re as _re
            m = _re.search(r"(\d+)", err)
            wait = m.group(1) if m else "?"
            await callback.message.answer(
                f"⏳ Слишком много запросов. Подождите <b>{wait} сек</b> и попробуйте снова.",
                parse_mode="HTML",
            )
            return
        # Code expired — restart login with a fresh SendCodeRequest
        try:
            new_hash, hint = await start_login(phone)
            await state.update_data(phone_code_hash=new_hash)
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ Отмена", callback_data=AccCb(action="cancel_login"))
            kb.adjust(1)
            await callback.message.answer(
                f"📱 Код запрошен заново.\n{hint} на <code>{escape(phone)}</code>.\n\nВведите код (только цифры):",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
            return
        except Exception as exc2:
            await callback.message.answer(
                f"❌ Не удалось выслать код: <code>{escape(str(exc2)[:200])}</code>",
                parse_mode="HTML",
            )
            return

    await state.update_data(phone_code_hash=new_hash)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AccCb(action="cancel_login"))
    kb.adjust(1)
    await callback.message.answer(
        f"{hint} на <code>{escape(phone)}</code>.\n\n"
        f"Введите код (только цифры):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AccCb.filter(F.action == "cancel_login"))
async def cb_cancel_login(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Аккаунты", callback_data=AccCb(action="menu"))
    await callback.message.edit_text("❌ Авторизация отменена.", reply_markup=kb.as_markup())


# ── QR Login ──────────────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "qr_login"))
async def cb_qr_login(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    bot: Bot,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id

    try:
        png = await start_qr_login(user_id)
    except Exception as exc:
        await callback.message.answer(
            f"❌ Не удалось запустить QR-вход: <code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AccCb(action="cancel_qr"))
    kb.adjust(1)

    msg = await callback.message.answer_photo(
        BufferedInputFile(png, filename="qr.png"),
        caption=(
            "🔲 <b>Войдите через QR-код</b>\n\n"
            "1. Откройте Telegram на телефоне или ПК\n"
            "2. <b>Настройки → Устройства → Подключить устройство</b>\n"
            "3. Наведите камеру на QR-код выше\n\n"
            "<i>Ожидаем сканирование (2 минуты)...</i>"
        ),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

    asyncio.create_task(
        _qr_wait_task(bot, user_id, msg.chat.id, msg.message_id, pool,
                      state.storage, state.key.bot_id),
        name=f"qr-wait-{user_id}",
    )


async def _qr_wait_task(
    bot: Bot,
    user_id: int,
    chat_id: int,
    message_id: int,
    pool: asyncpg.Pool,
    storage,
    bot_id: int,
) -> None:
    """Background task: wait for QR scan and finalize account connection."""
    from telethon.errors import SessionPasswordNeededError
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.context import FSMContext as _FSMContext

    try:
        session_str, info = await wait_qr_login(user_id, timeout=120.0)
    except asyncio.TimeoutError:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Обновить QR", callback_data=AccCb(action="qr_login"))
        kb.button(text="◀️ К аккаунтам", callback_data=AccCb(action="menu"))
        kb.adjust(1)
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption="⏰ QR-код истёк. Нажмите «Обновить QR» чтобы получить новый.",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            pass
        await cleanup_qr_pending(user_id)
        return
    except SessionPasswordNeededError:
        # Set FSM state so the password message handler catches the next input
        key = StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)
        ctx = _FSMContext(storage=storage, key=key)
        await ctx.set_state(QrLogin2FA.waiting_password)

        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=AccCb(action="cancel_qr"))
        kb.adjust(1)
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=(
                    "🔐 <b>Аккаунт защищён паролем 2FA</b>\n\n"
                    "Введите пароль двухфакторной аутентификации:"
                ),
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            pass
        # Client stays in _pending_qr — needed by confirm_qr_2fa()
        return
    except Exception as exc:
        try:
            await bot.send_message(
                chat_id,
                f"❌ Ошибка QR-входа: <code>{escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await cleanup_qr_pending(user_id)
        return

    try:
        await db.add_tg_account(
            pool,
            owner_id=user_id,
            phone=info.get("phone", f"id:{info['tg_user_id']}"),
            session_str=session_str,
            tg_user_id=info.get("tg_user_id"),
            first_name=info.get("first_name", ""),
            username=info.get("username", ""),
            device_model=info.get("device_model"),
            system_version=info.get("system_version"),
            app_version=info.get("app_version"),
        )
    except Exception as exc:
        try:
            await bot.send_message(
                chat_id,
                f"❌ Не удалось сохранить аккаунт: <code>{escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await cleanup_qr_pending(user_id)
        return

    await cleanup_qr_pending(user_id)

    display = escape(info.get("first_name") or info.get("username") or f"id:{info['tg_user_id']}")
    phone_str = escape(info.get("phone", ""))

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить ещё аккаунт", callback_data=AccCb(action="add"))
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=(
                f"✅ <b>Аккаунт успешно подключён!</b>\n\n"
                f"👤 {display}\n"
                f"📱 {phone_str}"
            ),
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await bot.send_message(
            chat_id,
            f"✅ <b>Аккаунт {display} подключён!</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


@router.callback_query(AccCb.filter(F.action == "cancel_qr"))
async def cb_cancel_qr(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    await cleanup_qr_pending(user_id)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Аккаунты", callback_data=AccCb(action="menu"))
    await callback.message.edit_text("❌ QR-вход отменён.", reply_markup=kb.as_markup())


@router.message(QrLogin2FA.waiting_password)
async def handle_qr_2fa(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    """Handle 2FA password after QR scan."""
    password = (message.text or "").strip()
    user_id = message.from_user.id

    try:
        session_str, info = await confirm_qr_2fa(user_id, password)
    except ValueError as exc:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=AccCb(action="cancel_qr"))
        kb.adjust(1)
        await message.answer(
            f"❌ {escape(str(exc))}\n\nВведите пароль ещё раз:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    except Exception as exc:
        await message.answer(
            f"❌ Ошибка: <code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        await state.clear()
        await cleanup_qr_pending(user_id)
        return

    await state.clear()
    await cleanup_qr_pending(user_id)

    try:
        await db.add_tg_account(
            pool,
            owner_id=user_id,
            phone=info.get("phone", f"id:{info['tg_user_id']}"),
            session_str=session_str,
            tg_user_id=info.get("tg_user_id"),
            first_name=info.get("first_name", ""),
            username=info.get("username", ""),
            device_model=info.get("device_model"),
            system_version=info.get("system_version"),
            app_version=info.get("app_version"),
        )
    except Exception as exc:
        await message.answer(
            f"❌ Не удалось сохранить аккаунт: <code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        return

    display = escape(info.get("first_name") or info.get("username") or f"id:{info['tg_user_id']}")
    phone_str = escape(info.get("phone", ""))

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить ещё аккаунт", callback_data=AccCb(action="add"))
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        f"✅ <b>Аккаунт успешно подключён!</b>\n\n"
        f"👤 {display}\n"
        f"📱 {phone_str}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
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
            device_model=info.get("device_model"),
            system_version=info.get("system_version"),
            app_version=info.get("app_version"),
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
    kb.button(text="➕ Добавить ещё аккаунт", callback_data=AccCb(action="add"))
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

    name = escape(acc.get("first_name") or "")
    uname = f"@{escape(acc['username'])}" if acc.get("username") else ""
    phone = escape(acc.get("phone") or "")
    tg_id = acc.get("tg_user_id") or ""
    is_active = bool(acc.get("is_active", True))

    proxy_url = acc.get("proxy_url") or ""
    proxy_label = acc.get("proxy_label") or ""
    proxy_line = f"🌐 {escape(proxy_label or proxy_url[:40])}" if proxy_url else "🌐 Без прокси"

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
    lines.append(f"Прокси: {proxy_line}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_acc_menu_markup(callback_data.acc_id, is_active=is_active),
    )


# ── Proxy assignment ──────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "set_proxy"))
async def cb_set_proxy(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    proxies = await pool.fetch(
        "SELECT id, label, proxy_url, is_alive FROM user_proxies "
        "WHERE owner_id=$1 AND is_active=TRUE ORDER BY label",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Без прокси", callback_data=AccCb(action="assign_proxy", acc_id=acc_id, page=0))
    for px in proxies[:10]:
        alive = "✅" if px["is_alive"] else ("❓" if px["is_alive"] is None else "❌")
        label = px["label"] or px["proxy_url"][:30]
        kb.button(
            text=f"{alive} {escape(label)}",
            callback_data=AccCb(action="assign_proxy", acc_id=acc_id, page=px["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=AccCb(action="view", acc_id=acc_id))
    kb.adjust(1)
    if not proxies:
        text = (
            "🌐 <b>Назначение прокси</b>\n\n"
            "У вас нет добавленных прокси.\n"
            "Добавьте прокси через <b>🏗️ Infrastructure → 🌐 Прокси</b>."
        )
    else:
        text = (
            f"🌐 <b>Назначение прокси для аккаунта #{acc_id}</b>\n\n"
            f"Выберите прокси (✅ живой, ❌ мёртвый, ❓ не проверен):\n"
            f"Текущий прокси будет сохранён немедленно."
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(AccCb.filter(F.action == "assign_proxy"))
async def cb_assign_proxy(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc_id = callback_data.acc_id
    proxy_id = callback_data.page  # 0 = no proxy, >0 = proxy id
    if proxy_id == 0:
        await pool.execute(
            "UPDATE tg_accounts SET proxy_id=NULL WHERE id=$1 AND owner_id=$2",
            acc_id, callback.from_user.id,
        )
        await callback.answer("✅ Прокси снят", show_alert=True)
    else:
        # Verify proxy belongs to this user
        px = await pool.fetchrow(
            "SELECT id FROM user_proxies WHERE id=$1 AND owner_id=$2",
            proxy_id, callback.from_user.id,
        )
        if not px:
            await callback.answer("Прокси не найден", show_alert=True)
            return
        await pool.execute(
            "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3",
            proxy_id, acc_id, callback.from_user.id,
        )
        await callback.answer("✅ Прокси назначен", show_alert=True)
    # Refresh account view
    acc = await db.get_tg_account(pool, acc_id, callback.from_user.id)
    if acc:
        is_active = bool(acc.get("is_active", True))
        proxy_url = acc.get("proxy_url") or ""
        proxy_label = acc.get("proxy_label") or ""
        proxy_line = f"🌐 {escape(proxy_label or proxy_url[:40])}" if proxy_url else "🌐 Без прокси"
        name = escape(acc.get("first_name") or "")
        lines = ["👤 <b>Аккаунт</b>\n"]
        if name:
            lines.append(f"Имя: <b>{name}</b>")
        lines.append(f"Статус: {'✅ Активен' if is_active else '⏸ Отключён'}")
        lines.append(f"Прокси: {proxy_line}")
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_acc_menu_markup(acc_id, is_active=is_active),
        )


# ── Channels / groups list ─────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "channels"))
async def cb_channels(
    callback: CallbackQuery,
    callback_data: AccCb,
    pool: asyncpg.Pool,
) -> None:
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text(
        "⏳ Загружаю список каналов и групп…",
        parse_mode="HTML",
    )

    try:
        dialogs = await get_dialogs(session_str, _acc=acc)
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text("⏳ Загружаю список каналов…", parse_mode="HTML")

    try:
        dialogs = await get_dialogs(session_str, _acc=acc)
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

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
        await send_message(session_str, chat_id, text, _acc=acc)
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text(
        "🔍 <b>Проверяю аккаунт…</b>",
        parse_mode="HTML",
    )

    try:
        result = await check_account_health(session_str, _acc=acc)
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

    session_str = acc.get("session_str") or acc.get("session_string") or ""

    await callback.message.edit_text(
        "📊 <b>Загружаю статистику диалогов…</b>",
        parse_mode="HTML",
    )

    try:
        stats = await get_account_dialogs_stats(session_str, _acc=acc)
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

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

    lines = ["👤 <b>Аккаунт</b>\n", status_text]
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
    acc = await db.get_tg_account(pool, callback_data.acc_id, callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()

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


# ── Session Import ─────────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "import_menu"))
async def cb_import_menu(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    if not _api_configured():
        await callback.message.edit_text(_api_missing_text(), parse_mode="HTML", reply_markup=_cancel_markup())
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
            f"⚠️ Достигнут лимит аккаунтов (<b>{plan.upper()}</b>: {limit_label}).\n\n"
            "Обновите подписку для добавления новых аккаунтов.",
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(plan),
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 String Session (Telethon)",  callback_data=AccCb(action="import_string"))
    kb.button(text="📄 Session JSON (Pyrogram)",    callback_data=AccCb(action="import_pyrogram"))
    kb.button(text="📦 tdata (ZIP-архив)",          callback_data=AccCb(action="import_tdata"))
    kb.button(text="◀️ Мои аккаунты",              callback_data=AccCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📥 <b>Импорт сессии</b>\n\n"
        "Выберите формат:\n\n"
        "🔑 <b>String Session</b> — строка вида <code>1BQANOTEuA...</code> (Telethon)\n"
        "📄 <b>Session JSON</b> — JSON с полями <code>dc_id</code>, <code>auth_key</code> (Pyrogram)\n"
        "📦 <b>tdata</b> — ZIP-архив папки <code>tdata</code> из Telegram Desktop\n\n"
        "⚠️ Никогда не передавайте сессии незнакомым людям.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Import: Telethon String Session ───────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "import_string"))
async def cb_import_string(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SessionImport.waiting_string_session)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AccCb(action="import_menu"))
    await callback.message.edit_text(
        "🔑 <b>Импорт Telethon String Session</b>\n\n"
        "Отправьте строку сессии. Она начинается с цифры <code>1</code> "
        "и содержит только буквы, цифры, <code>+</code>, <code>/</code>, <code>=</code>.\n\n"
        "Пример:\n<code>1BQANOTEuAGkA...</code>\n\n"
        "Как получить: запустить скрипт с <code>StringSession()</code> и вызвать <code>client.session.save()</code>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(SessionImport.waiting_string_session, F.text)
async def handle_import_string(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    session_str = (message.text or "").strip()
    msg = await message.answer("⏳ Проверяю сессию...")
    try:
        session_str, info = await import_from_session_string(session_str)
    except Exception as exc:
        await msg.edit_text(
            f"❌ <b>Ошибка импорта</b>\n\n<code>{escape(str(exc)[:300])}</code>\n\n"
            "Проверьте строку сессии и попробуйте снова.",
            parse_mode="HTML",
        )
        return
    await _finalize_import(message, pool, state, session_str, info)


# ── Import: Pyrogram JSON ─────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "import_pyrogram"))
async def cb_import_pyrogram(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SessionImport.waiting_pyrogram_json)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AccCb(action="import_menu"))
    await callback.message.edit_text(
        "📄 <b>Импорт Pyrogram JSON Session</b>\n\n"
        "Отправьте JSON с данными сессии. Необходимые поля:\n"
        "• <code>dc_id</code> — номер дата-центра (1–5)\n"
        "• <code>auth_key</code> — ключ авторизации (base64, 256 байт)\n\n"
        "Пример:\n"
        "<code>{\"dc_id\": 2, \"auth_key\": \"AAAA...\", \"user_id\": 123456}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(SessionImport.waiting_pyrogram_json, F.text)
async def handle_import_pyrogram(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    json_str = (message.text or "").strip()
    msg = await message.answer("⏳ Конвертирую и проверяю сессию...")
    try:
        session_str, info = await import_from_pyrogram_json(json_str)
    except Exception as exc:
        await msg.edit_text(
            f"❌ <b>Ошибка импорта</b>\n\n<code>{escape(str(exc)[:300])}</code>\n\n"
            "Проверьте JSON и попробуйте снова.",
            parse_mode="HTML",
        )
        return
    await _finalize_import(message, pool, state, session_str, info)


# ── Import: tdata ZIP ─────────────────────────────────────────────────────────

@router.callback_query(AccCb.filter(F.action == "import_tdata"))
async def cb_import_tdata(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SessionImport.waiting_tdata_zip)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AccCb(action="import_menu"))
    await callback.message.edit_text(
        "📦 <b>Импорт tdata (Telegram Desktop)</b>\n\n"
        "<b>Как подготовить архив:</b>\n"
        "1. Найдите папку <code>tdata</code> в директории Telegram Desktop\n"
        "   • Windows: <code>%APPDATA%\\Telegram Desktop\\tdata</code>\n"
        "   • Linux: <code>~/.local/share/TelegramDesktop/tdata</code>\n"
        "   • macOS: <code>~/Library/Group Containers/.../tdata</code>\n"
        "2. Упакуйте папку <code>tdata</code> целиком в ZIP-архив\n"
        "3. Отправьте ZIP-файл сюда\n\n"
        "⚠️ Максимальный размер: <b>20 МБ</b>\n"
        "⚠️ Аккаунт не должен быть защищён паролем экрана блокировки Telegram Desktop",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(SessionImport.waiting_tdata_zip, F.document)
async def handle_import_tdata(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    import os
    import tempfile
    import zipfile

    doc = message.document
    if not doc:
        await message.answer("⚠️ Отправьте ZIP-файл как документ.")
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("❌ Файл слишком большой. Максимум 20 МБ.")
        return

    name = (doc.file_name or "").lower()
    if not name.endswith(".zip"):
        await message.answer("❌ Ожидается ZIP-архив. Упакуйте папку tdata в .zip и отправьте снова.")
        return

    msg = await message.answer("⏳ Загружаю архив...")

    tmp_dir = tempfile.mkdtemp(prefix="tdata_import_")
    zip_path = os.path.join(tmp_dir, "tdata.zip")
    extract_dir = os.path.join(tmp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)

    try:
        # Download ZIP
        bot_file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(bot_file.file_path, destination=zip_path)

        # Extract
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            await msg.edit_text("❌ Файл повреждён или не является ZIP-архивом.")
            return

        # Locate tdata folder inside the extract
        tdata_path = _find_tdata_root(extract_dir)
        if not tdata_path:
            await msg.edit_text(
                "❌ Папка <code>tdata</code> не найдена в архиве.\n\n"
                "Убедитесь что архив содержит папку <code>tdata</code> с файлом <code>key_datas</code>.",
                parse_mode="HTML",
            )
            return

        await msg.edit_text("⏳ Конвертирую tdata в сессию Telethon...")
        try:
            session_str, info = await import_from_tdata(tdata_path)
        except ImportError as exc:
            await msg.edit_text(
                f"❌ <b>Пакет opentele не установлен</b>\n\n<code>{escape(str(exc)[:200])}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            await msg.edit_text(
                f"❌ <b>Ошибка конвертации tdata</b>\n\n<code>{escape(str(exc)[:300])}</code>",
                parse_mode="HTML",
            )
            return
    finally:
        # Always clean up temp files
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    await _finalize_import(message, pool, state, session_str, info)


@router.message(SessionImport.waiting_tdata_zip)
async def handle_import_tdata_wrong_type(message: Message) -> None:
    await message.answer(
        "⚠️ Отправьте ZIP-файл как документ (не фото, не текст).\n\n"
        "Используйте вложение → Файл при отправке."
    )


# ── Shared import finalization ─────────────────────────────────────────────────

def _find_tdata_root(extract_dir: str) -> str | None:
    """Walk the extraction directory and find the tdata root (contains key_datas)."""
    import os
    # Check up to 3 levels deep
    for root, dirs, files in os.walk(extract_dir):
        if "key_datas" in files:
            return root
        # Limit depth
        depth = root[len(extract_dir):].count(os.sep)
        if depth >= 3:
            dirs.clear()
    return None


async def _finalize_import(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
    session_str: str,
    info: dict,
) -> None:
    """Save imported account to DB and report success."""
    await state.clear()

    plan, limit = await _get_account_limit(pool, message.from_user.id)
    accounts = await db.get_tg_accounts(pool, message.from_user.id)
    if len(accounts) >= limit:
        limit_label = "∞" if limit >= 9999 else str(limit)
        await message.answer(
            f"⚠️ Достигнут лимит аккаунтов (<b>{plan.upper()}</b>: {limit_label}).\n\n"
            "Обновите подписку для добавления новых аккаунтов.",
            parse_mode="HTML",
        )
        return

    phone = info.get("phone") or f"id:{info.get('tg_user_id', 'unknown')}"
    device = generate_device_fingerprint()
    try:
        await db.add_tg_account(
            pool,
            owner_id=message.from_user.id,
            phone=phone,
            session_str=session_str,
            tg_user_id=info.get("tg_user_id") or 0,
            first_name=info.get("first_name", ""),
            username=info.get("username", ""),
            device_model=device["device_model"],
            system_version=device["system_version"],
            app_version=device["app_version"],
        )
    except Exception as exc:
        await message.answer(
            f"❌ Ошибка сохранения в БД: <code>{escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        return

    name = info.get("first_name", "") or info.get("username", "") or phone
    uname = f"@{info['username']}" if info.get("username") else "—"

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Импортировать ещё", callback_data=AccCb(action="import_menu"))
    kb.button(text="🔲 Добавить (QR-код)", callback_data=AccCb(action="qr_login"))
    kb.button(text="👤 Мои аккаунты", callback_data=AccCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Аккаунт успешно импортирован!</b>\n\n"
        f"Имя: <b>{escape(name)}</b>\n"
        f"Username: {escape(uname)}\n"
        f"Телефон: <code>{escape(phone)}</code>\n"
        f"Telegram ID: <code>{info.get('tg_user_id', '?')}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
