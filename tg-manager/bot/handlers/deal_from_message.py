"""«В сделку» прямо у сообщения клиента в боте.

Клиент пишет в бот владельца, бот пересылает сообщение владельцу
(services/auto_responder.py, релей). Раньше, чтобы завести по нему сделку,
нужно было открыть мини-апп, найти человека в контактах и поставить стадию —
три экрана ради одного действия, которое у лидеров (amoCRM, Bitrix24) делается
из самого диалога.

Теперь у пересланного сообщения есть кнопка «🗂 В сделку». Нажатие:
* находит или заводит контакт по Telegram-id клиента (одна дверь —
  ``ensure_contact_by_telegram_id``);
* ставит его в воронку стадией «Лид», если он ещё не в ней (стадия, которая
  уже есть, не сбрасывается);
* меняет кнопки этого же сообщения на выбор стадии — сообщение остаётся
  на месте, и ответ клиенту через Reply работает как прежде.

Стадию пишет ``crm_engine.upsert_crm`` — та же запись, что у канбана и
карточки клиента, поэтому сделка сразу видна в канбане «Сделки».
"""
from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import DealMsgCb
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

# Стадии в кнопках — коротко, чтобы три помещались в ряд.
_SHORT = {
    "lead": "Лид", "contact": "Контакт", "proposal": "Предложение",
    "negotiation": "Переговоры", "won": "✅ Выиграно", "lost": "✖️ Проиграно",
}


def add_button_markup(bot_id: int, chat_id: int) -> InlineKeyboardMarkup:
    """Кнопка для пересланного сообщения клиента."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗂 В сделку", callback_data=DealMsgCb(action="add", b=int(bot_id), u=int(chat_id)))
    return kb.as_markup()


def stage_markup(bot_id: int, chat_id: int, current: str) -> InlineKeyboardMarkup:
    from bot.handlers.botmother_menu import _valid_mini_app_url
    from config import MINI_APP_URL
    from services.contacts_hub.crm_engine import STAGE_LABELS

    kb = InlineKeyboardBuilder()
    for st in STAGE_LABELS:
        label = _SHORT.get(st, STAGE_LABELS[st])
        kb.button(text=("• " + label) if st == current else label,
                  callback_data=DealMsgCb(action="st", b=int(bot_id), u=int(chat_id), s=st))
    rows = [3, 3]
    url = _valid_mini_app_url(MINI_APP_URL)
    if url:
        from aiogram.types import WebAppInfo
        kb.button(text="🗂 Открыть канбан", web_app=WebAppInfo(url=f"{url}#deals"))
        rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


async def _peer(pool, owner_id: int, bot_id: int, chat_id: int) -> dict | None:
    """Клиент этого владельца: релей-сессия его бота. Чужой бот — None."""
    return await pool.fetchrow(
        "SELECT s.username, s.first_name FROM relay_sessions s "
        "JOIN managed_bots b ON b.bot_id = s.bot_id "
        "WHERE s.bot_id=$1 AND s.user_id=$2 AND b.added_by=$3 "
        "ORDER BY s.id DESC LIMIT 1", int(bot_id), int(chat_id), int(owner_id))


async def put_in_deals(pool, owner_id: int, bot_id: int, chat_id: int,
                       stage: str | None = None) -> str | None:
    """Завести/подвинуть сделку. Возвращает итоговую стадию или None, если
    клиент не этого владельца или контакт не завёлся."""
    from services.contacts_hub import crm_engine
    from services.contacts_hub.repository import ensure_contact_by_telegram_id

    peer = await _peer(pool, owner_id, bot_id, chat_id)
    if peer is None:
        return None
    cid = await ensure_contact_by_telegram_id(
        pool, owner_id, chat_id, username=peer["username"], first_name=peer["first_name"])
    if not cid:
        return None
    existing = await crm_engine.get_crm_data(pool, owner_id, str(cid))
    if stage is None:
        if existing and existing.get("stage"):
            return existing["stage"]       # уже в воронке — стадию не сбрасываем
        stage = "lead"
    await crm_engine.upsert_crm(pool, owner_id, str(cid), {
        "stage": stage, "last_interaction_type": "bot_message"})
    return stage


@router.callback_query(DealMsgCb.filter(F.action == "add"))
async def cb_deal_add(call: CallbackQuery, callback_data: DealMsgCb, pool: asyncpg.Pool) -> None:
    await _apply(call, callback_data, pool, None)


@router.callback_query(DealMsgCb.filter(F.action == "st"))
async def cb_deal_stage(call: CallbackQuery, callback_data: DealMsgCb, pool: asyncpg.Pool) -> None:
    from services.contacts_hub.crm_engine import STAGE_LABELS
    if callback_data.s not in STAGE_LABELS:
        await safe_answer(call, "Такой стадии нет", show_alert=True)
        return
    await _apply(call, callback_data, pool, callback_data.s)


async def _apply(call: CallbackQuery, cd: DealMsgCb, pool, stage: str | None) -> None:
    from services.contacts_hub.crm_engine import STAGE_LABELS
    try:
        got = await put_in_deals(pool, call.from_user.id, cd.b, cd.u, stage)
    except Exception:
        log.exception("deal_from_message owner=%s bot=%s user=%s", call.from_user.id, cd.b, cd.u)
        await safe_answer(call, "❌ Не получилось завести сделку, попробуйте ещё раз", show_alert=True)
        return
    if got is None:
        await safe_answer(call, "Этот клиент не найден среди диалогов ваших ботов", show_alert=True)
        return
    await safe_answer(call, f"🗂 В сделках: {STAGE_LABELS.get(got, got)}")
    try:
        await call.message.edit_reply_markup(reply_markup=stage_markup(cd.b, cd.u, got))
    except Exception:
        # Сообщение старое или кнопки те же — стадия уже записана, это главное.
        log.debug("deal_from_message: edit markup skipped", exc_info=True)
