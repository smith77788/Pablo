"""Заглушить проактивное уведомление организма прямо из самого уведомления.

Нудж (organism/runner) повторяется каждые 6 часов, и выключить его было нечем:
в мини-аппе есть только «отклонить навсегда», а в ЛС — вообще ничего. Человек с
одной незакрытой проблемой («Хранилище не пишет») получал одно и то же
сообщение сутками и терял доверие ко ВСЕМ уведомлениям, включая важные вроде
риска бана. Здесь — кнопки под сообщением: заглушить на срок или совсем.

Заглушка per-подсказка, не глобальная: «Хранилище не пишет» замолкает, а
«Критический риск бана» продолжает приходить.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
import asyncpg

from bot.callbacks import SnoozeCb
from bot.utils.op_helpers import terminal_kb

log = logging.getLogger(__name__)
router = Router()

# Часовой пояс для человекочитаемого «до …». Мини-апп и бот показывают МСК.
_TZ = timezone(timedelta(hours=3))


def format_until(until_ts: float) -> str:
    """Unix-время → «до 26.08 в 14:30» (МСК). Чистая функция — тестируется без БД."""
    dt = datetime.fromtimestamp(float(until_ts), _TZ)
    return f"до {dt.strftime('%d.%m')} в {dt.strftime('%H:%M')}"


@router.callback_query(SnoozeCb.filter(F.action == "mute"))
async def cb_snooze_mute(
    callback: CallbackQuery,
    callback_data: SnoozeCb,
    pool: asyncpg.Pool,
) -> None:
    from services.organism import brain

    uid = callback.from_user.id
    until = await brain.snooze(pool, uid, callback_data.sid, callback_data.code)
    if not until:
        await callback.answer("Не удалось заглушить — попробуйте ещё раз.", show_alert=True)
        return

    label = brain.snooze_label(callback_data.code)
    note = f"\n\n🔕 Заглушено на {label} — {format_until(until)}."
    try:
        await callback.message.edit_text(
            (callback.message.html_text or callback.message.text or "") + note,
            parse_mode="HTML",
        )
    except Exception:
        # Текст мог не измениться/сообщение устарело — тихо, факт заглушки уже записан.
        log.debug("cb_snooze_mute: edit failed uid=%s", uid, exc_info=True)
    await callback.answer(f"Заглушено на {label}")


@router.callback_query(SnoozeCb.filter(F.action == "off"))
async def cb_snooze_off(
    callback: CallbackQuery,
    callback_data: SnoozeCb,
    pool: asyncpg.Pool,
) -> None:
    """Больше не напоминать — переиспользуем существующий список «отклонённых»,
    тот же, что и кнопка отклонения в мини-аппе (один источник правды)."""
    from services.organism import spine

    uid = callback.from_user.id
    sid = callback_data.sid
    try:
        cur = await spine.state_get(pool, uid, "dismissed", []) or []
        if sid not in cur:
            cur.append(sid)
            await spine.state_set(pool, uid, "dismissed", cur[-100:])
    except Exception:
        log.warning("cb_snooze_off: сохранить не вышло uid=%s sid=%s", uid, sid, exc_info=True)
        await callback.answer("Не удалось сохранить — попробуйте ещё раз.", show_alert=True)
        return

    try:
        await callback.message.edit_text(
            (callback.message.html_text or callback.message.text or "")
            + "\n\n🚫 Больше не напоминать. Все уведомления сразу — /unmute",
            parse_mode="HTML",
            reply_markup=_undo_kb(sid),
        )
    except Exception:
        log.debug("cb_snooze_off: edit failed uid=%s", uid, exc_info=True)
    await callback.answer("Больше не напомню")


def _undo_kb(sid: str):
    """Кнопка отмены прямо на сообщении — защита от случайного нажатия.

    Без неё «Больше не напоминать» необратимо одним тапом: человек гасит важный
    сигнал и узнаёт об этом, только когда проблема выстрелит.
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Вернуть это уведомление",
              callback_data=SnoozeCb(action="on", sid=sid, code=""))
    return kb.as_markup()


@router.callback_query(SnoozeCb.filter(F.action == "on"))
async def cb_snooze_undo(
    callback: CallbackQuery,
    callback_data: SnoozeCb,
    pool: asyncpg.Pool,
) -> None:
    """Вернуть ОДНО уведомление: снять и заглушку, и отклонение по этому id."""
    from services.organism import brain, spine

    uid = callback.from_user.id
    sid = callback_data.sid
    try:
        cur = await spine.state_get(pool, uid, "dismissed", []) or []
        if sid in cur:
            await spine.state_set(pool, uid, "dismissed", [x for x in cur if x != sid])
        snz = await spine.state_get(pool, uid, brain.SNOOZE_KEY, {}) or {}
        if sid in snz:
            snz.pop(sid, None)
            await spine.state_set(pool, uid, brain.SNOOZE_KEY, snz)
    except Exception:
        log.warning("cb_snooze_undo failed uid=%s sid=%s", uid, sid, exc_info=True)
        await callback.answer("Не удалось вернуть — попробуйте ещё раз.", show_alert=True)
        return

    try:
        base = (callback.message.html_text or callback.message.text or "")
        for tail in ("\n\n🚫", "\n\n🔕"):
            base = base.split(tail)[0]
        await callback.message.edit_text(
            base + "\n\n🔔 Уведомление возвращено.", parse_mode="HTML")
    except Exception:
        log.debug("cb_snooze_undo: edit failed uid=%s", uid, exc_info=True)
    await callback.answer("Вернул")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, pool: asyncpg.Pool) -> None:
    """Вернуть все заглушённые и отклонённые уведомления.

    Без этого «Больше не напоминать» было бы ловушкой без выхода: человек в один
    тап навсегда терял важный сигнал и не имел способа его вернуть. Команда —
    самый дешёвый честный путь назад (в мини-аппе экрана восстановления нет).
    """
    from services.organism import brain, spine

    uid = message.from_user.id
    try:
        snoozed = await spine.state_get(pool, uid, brain.SNOOZE_KEY, {}) or {}
        dismissed = await spine.state_get(pool, uid, "dismissed", []) or []
        n = len(snoozed) + len(dismissed)
        await spine.state_set(pool, uid, brain.SNOOZE_KEY, {})
        await spine.state_set(pool, uid, "dismissed", [])
    except Exception:
        log.warning("cmd_unmute failed uid=%s", uid, exc_info=True)
        await message.answer("⚠️ Не удалось вернуть уведомления — попробуйте позже.",
                             reply_markup=terminal_kb())
        return

    if not n:
        await message.answer("🔔 Заглушённых уведомлений нет — всё и так приходит.",
                             reply_markup=terminal_kb())
        return
    await message.answer(
        f"🔔 Вернул {n} уведомлени(й). Снова буду предупреждать о проблемах.",
        reply_markup=terminal_kb())
