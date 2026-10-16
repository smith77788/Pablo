"""Экран «Что такое Infragram» — чтобы не объяснять проект каждому вручную.

ЗАЧЕМ. Владельца регулярно спрашивают, что это за проект и что он умеет, и
каждый раз приходится пересказывать. Объяснение должно жить в самом продукте:
кнопка в меню, разделы с подробностями и ГОТОВОЕ сообщение, которое можно
переслать одним движением.

Три вещи, ради которых это сделано именно так:
  • «Переслать знакомому» отдаёт ОДНО самодостаточное сообщение со ссылкой
    внутри. Пересылают всегда одно сообщение, а кнопки под ним у получателя не
    сработают — поэтому ссылка, а не кнопка;
  • ссылка ведёт на `?start=about`, то есть получатель открывает то же
    описание сам и больше ничего не спрашивает;
  • текст берётся из services.product_catalog — одного источника, который
    сверяется тестом с каталогом функций мини-аппа.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AboutCb, BotCb
from bot.utils.op_helpers import safe_answer
from services import product_catalog as catalog
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

router = Router()

# Telegram режет сообщения длиннее 4096 символов. Текст каталога растёт вместе с
# продуктом, поэтому режем сами и предсказуемо, а не отдаём Telegram шанс
# отклонить отправку целиком.
_TG_LIMIT = 4096


def _clip(text: str) -> str:
    if len(text) <= _TG_LIMIT:
        return text
    return text[: _TG_LIMIT - 1].rstrip() + "…"


def _menu_kb() -> "InlineKeyboardBuilder":
    kb = InlineKeyboardBuilder()
    for sec in catalog.SECTIONS:
        kb.button(text=f"{sec.icon} {sec.title}",
                  callback_data=AboutCb(action="sec", key=sec.key))
    kb.button(text="📤 Переслать знакомому",
              callback_data=AboutCb(action="share"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    # Разделы по два в ряд, две последние кнопки — на всю ширину.
    kb.adjust(*([2] * ((len(catalog.SECTIONS) + 1) // 2)), 1, 1)
    return kb


def _section_kb(key: str) -> "InlineKeyboardBuilder":
    """Клавиатура раздела: соседние разделы + возврат.

    Пустых экранов быть не должно: из любого раздела видно, куда идти дальше.
    """
    kb = InlineKeyboardBuilder()
    keys = [s.key for s in catalog.SECTIONS]
    try:
        i = keys.index(key)
    except ValueError:
        i = 0
    prev_s = catalog.SECTIONS[(i - 1) % len(catalog.SECTIONS)]
    next_s = catalog.SECTIONS[(i + 1) % len(catalog.SECTIONS)]
    kb.button(text=f"◀️ {prev_s.title}",
              callback_data=AboutCb(action="sec", key=prev_s.key))
    kb.button(text=f"{next_s.title} ▶️",
              callback_data=AboutCb(action="sec", key=next_s.key))
    kb.button(text="📤 Переслать знакомому", callback_data=AboutCb(action="share"))
    kb.button(text="🗂 Все разделы", callback_data=AboutCb(action="menu"))
    kb.adjust(2, 1, 1)
    return kb


async def _bot_username(message_or_cb) -> str:
    """@username бота — для ссылки в пересылаемом сообщении.

    Без него ссылку не построить, но и падать нельзя: текст полезен и без неё.
    """
    try:
        me = await message_or_cb.bot.get_me()
        return me.username or ""
    except Exception:
        log_exc_swallow(log, "about: не удалось узнать username бота")
        return ""


async def _send_intro(message: Message) -> None:
    await message.answer(
        _clip(catalog.intro_text()),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_menu_kb().as_markup(),
    )


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await _send_intro(message)


@router.callback_query(AboutCb.filter(F.action == "menu"))
async def cb_about_menu(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    try:
        await callback.message.edit_text(
            _clip(catalog.intro_text()),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_menu_kb().as_markup(),
        )
    except Exception:
        # Сообщение могло быть не редактируемым (переслано, слишком старое) —
        # тогда просто отправляем новое, а не оставляем человека без ответа.
        await _send_intro(callback.message)


@router.callback_query(AboutCb.filter(F.action == "sec"))
async def cb_about_section(callback: CallbackQuery, callback_data: AboutCb) -> None:
    await safe_answer(callback)
    sec = catalog.section_by_key(callback_data.key)
    if sec is None:
        await cb_about_menu(callback)
        return
    try:
        await callback.message.edit_text(
            _clip(catalog.section_text(sec)),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_section_kb(sec.key).as_markup(),
        )
    except Exception:
        await callback.message.answer(
            _clip(catalog.section_text(sec)),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_section_kb(sec.key).as_markup(),
        )


@router.callback_query(AboutCb.filter(F.action == "share"))
async def cb_about_share(callback: CallbackQuery) -> None:
    """Отдать готовое сообщение для пересылки — ОТДЕЛЬНЫМ сообщением.

    Именно отдельным: пересылается одно сообщение целиком, и в нём не должно
    быть ни кнопок навигации (у получателя они не работают), ни лишних
    подсказок вроде «нажмите раздел».
    """
    await safe_answer(callback, "Готово — перешлите сообщение ниже")
    username = await _bot_username(callback)
    await callback.message.answer(
        _clip(catalog.share_text(username)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    # И сразу возвращаем управление: после пересылки человек остаётся в боте.
    kb = InlineKeyboardBuilder()
    kb.button(text="🗂 Все разделы", callback_data=AboutCb(action="menu"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(1, 1)
    await callback.message.answer(
        "⬆️ Перешлите сообщение выше — в нём есть ссылка, "
        "по которой человек откроет описание сам.",
        reply_markup=kb.as_markup(),
    )
