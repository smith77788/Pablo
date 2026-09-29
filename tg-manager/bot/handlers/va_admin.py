"""Виртуальный администратор канала — дверь бота.

Черновик поста (режим «на одобрение», или пост с замечаниями редактора после
«Опубликовать сейчас») приходит владельцу сообщением с кнопками. Решение идёт в
те же функции services/channel_admin, что и в Mini App: одна логика — две двери.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import VaCb
from services import channel_admin as ca

log = logging.getLogger(__name__)
router = Router()


async def _done(callback: CallbackQuery, note: str) -> None:
    """Оставить текст поста и дописать итог вместо кнопок."""
    try:
        base = callback.message.html_text or ""
        await callback.message.edit_text(base + "\n\n" + note, parse_mode="HTML",
                                         reply_markup=None)
    except Exception:
        try:
            await callback.message.answer(note, parse_mode="HTML")
        except Exception:
            log.debug("va_admin: итог не показан", exc_info=True)


@router.callback_query(VaCb.filter(F.action == "pub"))
async def va_publish(callback: CallbackQuery, callback_data: VaCb, pool) -> None:
    await callback.answer("Публикую…")
    try:
        res = await ca.publish_draft(pool, callback.from_user.id, callback_data.id)
    except ca.ChannelAdminError as e:
        await _done(callback, f"⚠️ {html.escape(str(e))}")
        return
    except Exception:
        log.exception("va_publish failed")
        await _done(callback, "⚠️ Не удалось поставить публикацию, попробуйте ещё раз.")
        return
    await _done(callback, f"✅ Отправлено в канал (операция №{res['op_id']}).")


@router.callback_query(VaCb.filter(F.action == "skip"))
async def va_skip(callback: CallbackQuery, callback_data: VaCb, pool) -> None:
    try:
        await ca.reject_draft(pool, callback.from_user.id, callback_data.id)
    except ca.ChannelAdminError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer("Пропущено")
    await _done(callback, "✖️ Пропущено. Следующий пост будет по плану.")


@router.callback_query(VaCb.filter(F.action == "regen"))
async def va_regen(callback: CallbackQuery, callback_data: VaCb, pool) -> None:
    await callback.answer("Пишу другой вариант…")
    try:
        d = await ca.regenerate_draft(pool, callback.from_user.id, callback_data.id)
    except ca.ChannelAdminError as e:
        await _done(callback, f"⚠️ {html.escape(str(e))}")
        return
    except Exception:
        log.exception("va_regen failed")
        await _done(callback, "⚠️ Не удалось написать другой вариант, попробуйте ещё раз.")
        return
    await _done(callback, "🔄 Заменён новым вариантом ниже.")
    await callback.message.answer(
        ca.draft_message(d.get("title") or "канал", d["pillar"], d["body"], d["reasons"]),
        parse_mode="HTML", reply_markup=ca.draft_keyboard(d["id"]),
    )
