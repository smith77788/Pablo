"""Виртуальный администратор канала — дверь бота.

Черновик поста (режим «на одобрение», или пост с замечаниями редактора после
«Опубликовать сейчас») приходит владельцу сообщением с кнопками. Решение идёт в
те же функции services/channel_admin, что и в Mini App: одна логика — две двери.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.callbacks import VaCb
from services import channel_admin as ca

log = logging.getLogger(__name__)
router = Router()


@router.message(Command("va_photo"), F.chat.type == "private")
async def va_add_photo(message: Message, pool) -> None:
    """Фото с явным разрешением владельца использовать его для конкретного канала."""
    from services import va_media
    parts = (message.caption or message.text or "").split(maxsplit=2)
    try:
        if not message.photo or len(parts) != 3:
            raise va_media.MediaError(
                "Отправьте фото с подписью: /va_photo ID_КАНАЛА описание изображения. "
                "Добавляйте только свои или разрешённые для публикации фото. "
                "Отправка разрешает администратору использовать фото в этом канале.")
        try:
            channel_id = int(parts[1])
        except ValueError as exc:
            raise va_media.MediaError("ID канала должен быть числом из экрана администратора") from exc
        photo = message.photo[-1]
        if not photo.file_size or photo.file_size > va_media.MAX_PHOTO_BYTES:
            raise va_media.MediaError("Нужна фотография размером не более 5 МБ")
        item = await va_media.add_photo(pool, message.from_user.id, channel_id, photo.file_id,
                                        photo.file_unique_id, parts[2])
    except va_media.MediaError as exc:
        await message.answer(str(exc), parse_mode=None)
        return
    await message.answer(f"Фото №{item['id']} добавлено в медиатеку канала. "
                         "Проверить и отключить его можно на экране администратора.", parse_mode=None)


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
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    for code, label in ca.REJECT_REASONS.items():
        kb.button(text=label, callback_data=VaCb(action="why", id=callback_data.id, r=code))
    kb.adjust(2)
    try:
        await callback.message.edit_text(
            (callback.message.html_text or "") + "\n\n✖️ Пропущено. Почему? Администратор "
            "учтёт это в следующих постах.", parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await _done(callback, "✖️ Пропущено. Следующий пост будет по плану.")


@router.callback_query(VaCb.filter(F.action == "why"))
async def va_why(callback: CallbackQuery, callback_data: VaCb, pool) -> None:
    ok = await ca.set_reject_reason(pool, callback.from_user.id, callback_data.id, callback_data.r)
    await callback.answer("Учту" if ok else "Черновик уже обработан")
    label = ca.REJECT_REASONS.get(callback_data.r, "")
    await _done(callback, f"Учту: {html.escape(label)}." if ok and label else "Следующий пост будет по плану.")


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
    from services import va_media
    photo = await va_media.draft_photo(pool, callback.from_user.id, d["id"])
    if photo:
        await callback.message.answer_photo(photo["file_id"], caption="Изображение к черновику ниже")
    await callback.message.answer(
        ca.draft_message(d.get("title") or "канал", d["pillar"], d["body"], d["reasons"]),
        parse_mode="HTML", reply_markup=ca.draft_keyboard(d["id"]),
    )
