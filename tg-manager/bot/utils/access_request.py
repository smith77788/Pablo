"""Запрос доступа к платному модулю, когда автоматическая оплата не настроена.

Раньше «⚔️ Массовые жалобы» и «🖥️ Host-Server» без `TRON_WALLET` писали
«Свяжитесь с администратором» — без кнопки и без контакта, то есть тупик:
человек готов платить, а сделать ничего не может. Здесь тот же механизм,
что у подписки («📩 Запросить подписку»): заявка уходит платформенным
админам, человек видит, что она отправлена.

Получатели — `platform_alert_recipients`, а не голый `ADMIN_IDS`: при
незаполненном `ADMIN_IDS` (обычная установка) заявку иначе не получил бы
никто, хотя вошедший по секрету админ есть.
"""
from __future__ import annotations

import html
import logging

from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


def user_label(callback: CallbackQuery) -> str:
    """Подпись пользователя для сообщения админу (экранирована: её пишет он сам)."""
    u = callback.from_user
    raw = f"@{u.username}" if u.username else (u.first_name or str(u.id))
    return html.escape(raw)


async def send_access_request(
    callback: CallbackQuery,
    pool,
    notify_text: str,
    admin_markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Отправить заявку всем платформенным админам. Возвращает число доставок.

    0 означает «заявку не получил никто» — вызывающий обязан сказать это
    человеку, а не печатать «Запрос отправлен».
    """
    from bot.utils.subscription import platform_alert_recipients

    try:
        recipients = await platform_alert_recipients(pool)
    except Exception:
        log_exc_swallow(log, "access_request: получатели не прочитались")
        recipients = set()
    delivered = 0
    for admin_id in sorted(recipients):
        try:
            await callback.bot.send_message(
                admin_id, notify_text, parse_mode="HTML", reply_markup=admin_markup,
            )
            delivered += 1
        except Exception:
            log_exc_swallow(log, "access_request: не доставлено админу", admin_id=admin_id)
    if not delivered:
        log.warning(
            "access request not delivered: no reachable admin user_id=%s",
            callback.from_user.id,
        )
    return delivered


NOT_DELIVERED_TEXT = (
    "⚠️ <b>Заявку пока некому принять</b>\n\n"
    "Администратор ещё не настроил приём заявок. "
    "Попробуйте позже или напишите владельцу сервиса напрямую."
)
