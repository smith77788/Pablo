"""Связки модулей в боте: кнопки «следующий шаг» под итогом операции.

Шаги считает `services/op_chain` — тот же источник, что у мини-аппа. Здесь
только отрисовка в клавиатуру и обработчик нажатия:

* шаг-запуск — кнопка ChainCb, нажатие уходит в `op_chain.launch` (цели
  берутся из сохранённого итога операции на сервере, не из кнопки);
* шаг-экран — родная кнопка бота, если у шага есть путь в боте, иначе
  web_app-кнопка в нужный раздел мини-аппа (`#kind:param`, тот же маршрут,
  что у плашки итога в приложении).
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import ChainCb, InviterCb
from services import op_chain

log = logging.getLogger(__name__)
router = Router(name="op_chain")


def _native_button(bot_spec: dict):
    """Родной путь шага в боте: (callback_data) или None."""
    cb = (bot_spec or {}).get("cb")
    if cb == "inviter_from_run":
        return InviterCb(action="from_run", item=str(int(bot_spec["arg"])))
    return None


def _webapp(screen: str):
    try:
        from aiogram.types import WebAppInfo
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from config import MINI_APP_URL

        url = _valid_mini_app_url(MINI_APP_URL)
        return WebAppInfo(url=f"{url}#{screen}") if url else None
    except Exception:
        return None


def add_chain_buttons(kb, op_id: int, steps: list[dict]) -> int:
    """Дописать в InlineKeyboardBuilder кнопки шагов. Возвращает, сколько добавлено."""
    n = 0
    for st in steps or []:
        try:
            if st["kind"] == "launch":
                kb.button(text=st["label"],
                          callback_data=ChainCb(action="go", op_id=int(op_id), s=st["id"]))
                n += 1
                continue
            native = _native_button(st.get("bot"))
            if native is not None:
                kb.button(text=st["label"], callback_data=native)
                n += 1
                continue
            wa = _webapp(st.get("screen") or "")
            if wa is not None:
                kb.button(text=st["label"], web_app=wa)
                n += 1
        except Exception:
            log.debug("op_chain: кнопка шага %s не собрана", st.get("id"), exc_info=True)
    return n


@router.callback_query(ChainCb.filter(F.action == "go"))
async def chain_go(callback: CallbackQuery, callback_data: ChainCb, pool) -> None:
    await callback.answer("Запускаю…")
    r = await op_chain.launch(pool, callback.from_user.id, callback_data.op_id, callback_data.s)
    if r.get("ok"):
        ids = r.get("op_ids") or []
        tail = f" (операция №{ids[0]})" if len(ids) == 1 else (
            f" (операций: {len(ids)})" if ids else "")
        note = f"✅ {html.escape(r.get('message') or 'Запущено')}{tail}"
    else:
        note = f"⚠️ {html.escape(r.get('reason') or 'Шаг не запустился')}"
    try:
        await callback.message.answer(note, parse_mode="HTML")
    except Exception:
        log.debug("op_chain: итог запуска не показан", exc_info=True)
