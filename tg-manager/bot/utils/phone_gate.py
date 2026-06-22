"""Phone Gate — верификация платформ-пользователя по телефону Telegram.

Механизм: request_contact — Telegram сам гарантирует что пользователь
делится именно своим номером, а не чужим. Подделка невозможна.

Один verified_phone = один платформ-аккаунт.
При попытке верификации с уже занятым номером — блок.
"""

from __future__ import annotations

import logging

import asyncpg
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

log = logging.getLogger(__name__)

_GATE_TEXT = (
    "🔐 <b>Подтверждение личности</b>\n\n"
    "BotMother требует одноразовую верификацию вашего номера телефона.\n\n"
    "Это нужно для защиты от создания множественных аккаунтов. "
    "Один номер — один аккаунт.\n\n"
    "Нажмите кнопку ниже, чтобы поделиться номером.\n"
    "<i>Telegram сам подтвердит что это ваш номер.</i>"
)


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def is_phone_verified(pool: asyncpg.Pool, user_id: int) -> bool:
    """Проверить, верифицирован ли телефон пользователя."""
    try:
        val = await pool.fetchval(
            "SELECT verified_phone FROM platform_users WHERE user_id=$1", user_id
        )
        return bool(val)
    except Exception:
        return True  # при ошибке не блокируем


async def require_phone_verified(
    pool: asyncpg.Pool,
    event: Message | CallbackQuery,
) -> bool:
    """Проверить верификацию. Если нет — показать запрос и вернуть False.

    Возвращает True если верифицирован (можно продолжать).
    Возвращает False если нет — показывает кнопку, caller должен return.
    """
    from bot.utils.subscription import is_platform_admin

    target = event if isinstance(event, Message) else event.message
    user_id = (
        event.from_user.id
        if isinstance(event, Message)
        else event.from_user.id
    )

    if is_platform_admin(user_id):
        return True

    if await is_phone_verified(pool, user_id):
        return True

    if isinstance(event, CallbackQuery):
        await event.answer()

    await target.answer(
        _GATE_TEXT,
        parse_mode="HTML",
        reply_markup=_contact_keyboard(),
    )
    return False


async def save_verified_phone(
    pool: asyncpg.Pool, user_id: int, phone: str
) -> tuple[bool, str]:
    """Сохранить верифицированный телефон. Возвращает (ok, error_msg).

    ok=True — телефон уникален и сохранён.
    ok=False — телефон уже занят другим аккаунтом.
    """
    phone = phone.strip()
    if not phone:
        return False, "Пустой номер телефона"

    # Нормализуем: убедимся что начинается с +
    if not phone.startswith("+"):
        phone = f"+{phone}"

    try:
        await pool.execute(
            "UPDATE platform_users SET verified_phone=$1 WHERE user_id=$2",
            phone, user_id,
        )
        return True, ""
    except Exception as exc:
        # UniqueViolationError — номер уже занят
        err = str(exc)
        if "unique" in err.lower() or "duplicate" in err.lower() or "23505" in err:
            log.warning(
                "phone_gate: phone=%s already verified under another user, blocked user_id=%d",
                phone, user_id,
            )
            return False, phone
        log.exception("phone_gate: save_verified_phone failed: %s", exc)
        return False, ""
