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
from services import account_verdict, fleet_pulse

from bot.callbacks import BotCb
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_MAX_LISTED = 20   # не заваливаем экран — показываем самых «требующих внимания»


def _render(states: list[dict]) -> str:
    s = fleet_pulse.summarize(states)
    if not s["total"]:
        return ("💓 <b>Пульс флота</b>\n\nАктивных аккаунтов нет. Подключите "
                "аккаунты в разделе «TG-аккаунты».")
    # Та же одна метка, что на экране аккаунтов мини-аппа (account_verdict):
    # бот и мини-апп не имеют права называть один аккаунт по-разному.
    verdicts = [_verdict(a) for a in states]
    vs = account_verdict.summarize(verdicts)
    head = (
        "💓 <b>Пульс флота</b>\n\n"
        f"Работают: <b>{vs['ok']}</b> · Пауза: <b>{vs['pause']}</b> · "
        f"Нужно действие: <b>{vs['action']}</b>"
        + (f" · Потеряны: <b>{vs['lost']}</b>" if vs["lost"] else "") + "\n"
        f"<i>всего активных: {s['total']}</i>"
    )
    # Показываем НЕ готовых (им и нужен экран) и тех, кто готов с оговоркой:
    # аккаунт с доверием ниже порога операции не возьмут, и промолчать об этом
    # значит снова сказать «весь флот готов» перед тем, как операция обработает
    # ноль целей.
    busy = [a for a in states if a["state"] != "ready"]
    limited = [a for a in states if a["state"] == "ready" and a.get("hint")]
    if not busy and not limited:
        return head + "\n\n🎉 Весь флот готов к действиям — пауз нет."
    lines = []
    if busy:
        lines += ["", "<b>Кому нужна пауза или починка</b>"]
        for a in busy[:_MAX_LISTED]:
            name = html.escape(str(a["name"])[:24])
            tail = f" · через {a['ready_in']}" if a.get("ready_in") else ""
            v = _verdict(a)
            label = _VERDICT_ICON.get(v["level"], "") + " " + v["label"] if v else a["state_label"]
            lines.append(
                f"{label} <b>{name}</b>{tail}\n   <i>{a['reason']}</i>")
        if len(busy) > _MAX_LISTED:
            lines.append(f"\n… и ещё {len(busy) - _MAX_LISTED}")
    if limited:
        lines += ["", "<b>Готовы, но не для всего</b>"]
        for a in limited[:_MAX_LISTED]:
            name = html.escape(str(a["name"])[:24])
            lines.append(f"⚠️ <b>{name}</b>\n   <i>{a['hint']}</i>")
        if len(limited) > _MAX_LISTED:
            lines.append(f"\n… и ещё {len(limited) - _MAX_LISTED}")
    return head + "\n".join(lines)


_VERDICT_ICON = {"ok": "✅", "pause": "⏳", "action": "🔧", "lost": "⛔️", "off": "▫️"}


def _verdict(a: dict) -> dict | None:
    """Метка аккаунта из пульса: в пульсе только включённые аккаунты."""
    return account_verdict.verdict_for(
        {"acc_status": a.get("acc_status"), "is_active": True}, a)


def _kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=BotCb(action="pulse"))
    kb.button(text="🏠 Главное меню", callback_data=BotCb(action="main"))
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
