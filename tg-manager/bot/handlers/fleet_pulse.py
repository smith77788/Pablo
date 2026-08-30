"""Экран «Пульс флота»: почему аккаунты стоят и когда продолжат (ось №1 аудита).

Владельца бесит не пауза, а её невидимость: операция «ничего не делает», а
причина — в логах. Здесь она на экране: сводка по состояниям + список аккаунтов
на паузе с временем до готовности.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb
from bot.utils.op_helpers import safe_answer
from services import fleet_pulse

log = logging.getLogger(__name__)
router = Router()

_MAX_LISTED = 20   # не заваливаем экран — показываем самых «требующих внимания»


def _render(states: list[dict]) -> str:
    s = fleet_pulse.summarize(states)
    if not s["total"]:
        return ("💓 <b>Пульс флота</b>\n\nАктивных аккаунтов нет. Подключите "
                "аккаунты в разделе «TG-аккаунты».")
    head = (
        "💓 <b>Пульс флота</b>\n\n"
        f"✅ готовы: <b>{s['ready']}</b>   ⏳ пауза: <b>{s['cooling']}</b>   "
        f"🚑 карантин: <b>{s['quarantine']}</b>   ⛔️ выбыли: <b>{s['dead']}</b>\n"
        f"<i>всего активных: {s['total']}</i>"
    )
    # Показываем НЕ готовых (им и нужен экран); если все готовы — так и говорим.
    busy = [a for a in states if a["state"] != "ready"]
    if not busy:
        return head + "\n\n🎉 Весь флот готов к действиям — пауз нет."
    lines = ["", "<b>Кому нужна пауза</b>"]
    for a in busy[:_MAX_LISTED]:
        name = html.escape(str(a["name"])[:24])
        tail = f" · через {a['ready_in']}" if a.get("ready_in") else ""
        lines.append(f"{a['state_label']} <b>{name}</b>{tail}\n   <i>{a['reason']}</i>")
    if len(busy) > _MAX_LISTED:
        lines.append(f"\n… и ещё {len(busy) - _MAX_LISTED}")
    return head + "\n".join(lines)


def _kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=BotCb(action="pulse"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(2)
    return kb.as_markup()


async def _show(target, pool, owner_id: int, *, edit: bool) -> None:
    try:
        states = await fleet_pulse.account_states(pool, owner_id)
    except Exception:
        log.exception("fleet_pulse: сбор состояний упал owner=%s", owner_id)
        text = "💓 <b>Пульс флота</b>\n\n⚠️ Не удалось собрать состояние — попробуйте ещё раз."
        states = None
    else:
        text = _render(states)
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=_kb())
            return
        except Exception:
            pass
    await target.answer(text, parse_mode="HTML", reply_markup=_kb())


@router.message(Command("pulse"))
async def cmd_pulse(message: Message, pool) -> None:
    await _show(message, pool, message.from_user.id, edit=False)


@router.callback_query(BotCb.filter(F.action == "pulse"))
async def cb_pulse(callback: CallbackQuery, pool) -> None:
    await safe_answer(callback)
    await _show(callback.message, pool, callback.from_user.id, edit=True)
