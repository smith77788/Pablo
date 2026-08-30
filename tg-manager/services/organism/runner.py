"""Жизненный цикл организма: сердцебиение + проактивные нуджи.

Раньше «мозг» существовал только когда пользователь открывал экран. Здесь у
организма появляется ПУЛЬС: раз в N минут он смотрит на мир каждого владельца и,
если видит срочное (хранилище отвалилось, флот под давлением, операция упала),
сам пишет в ЛС — с кулдауном, чтобы не спамить. Так система «живёт» и «понимает»
между сессиями, а не ждёт, пока её откроют.

Кулдаун и антиспам держим в общем состоянии (organism_state.last_nudge).
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

INTERVAL = 15 * 60          # сердцебиение — раз в 15 мин
_SAME_COOLDOWN = 6 * 3600   # та же подсказка не чаще раза в 6ч
_ANY_GAP = 2 * 3600         # любой нудж не чаще раза в 2ч
_NUDGE_SEV = ("urgent", "warn")
_last_prune = 0.0          # троттл ретеншена журнала (не чаще раза в 6ч)


async def run(pool, bot) -> None:
    log.info("organism.runner: starting (heartbeat %ds)", INTERVAL)
    while True:
        try:
            await _tick(pool, bot)
        except Exception:
            log.exception("organism.runner: tick failed")
        await asyncio.sleep(INTERVAL)


async def _active_owners(pool) -> list[int]:
    rows = await pool.fetch(
        "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active AND owner_id IS NOT NULL")
    return [int(r["owner_id"]) for r in rows]


async def _tick(pool, bot) -> int:
    """Один проход по всем владельцам. Возвращает число отправленных нуджей."""
    global _last_prune
    now = time.time()
    if now - _last_prune > 6 * 3600:      # ретеншен журнала — не чаще раза в 6ч
        _last_prune = now
        try:
            from services.organism import spine
            await spine.prune_events(pool, days=30)
        except Exception:
            pass
    sent = 0
    for oid in await _active_owners(pool):
        try:
            if await _tick_owner(pool, bot, oid):
                sent += 1
        except Exception:
            log.debug("organism.runner: owner %s failed", oid, exc_info=True)
    if sent:
        log.info("organism.runner: нуджей отправлено %d", sent)
    return sent


def _open_button(action: dict | None):
    """web_app-кнопка «Открыть» — прямо в нужный раздел мини-аппа.

    Без неё нудж был тупиком: «ответьте клиенту», но чтобы ПРОЧИТАТЬ, надо вручную
    открыть приложение и искать раздел. Ведём на URL#<kind>; мини-апп разворачивает
    kind тем же runPulseAction, что и карточки Пульса. Возвращает (text, WebAppInfo)
    или None, если URL мини-аппа не настроен.
    """
    kind = (action or {}).get("kind")
    if not kind:
        return None
    try:
        from aiogram.types import WebAppInfo
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from config import MINI_APP_URL
        url = _valid_mini_app_url(MINI_APP_URL)
        if not url:
            return None
        return ("👉 Открыть и ответить" if kind == "vault" else "👉 Открыть",
                WebAppInfo(url=f"{url}#{kind}"))
    except Exception:
        return None


def _snooze_kb(suggestion_id: str, action: dict | None = None):
    """Кнопка «Открыть» (в нужный раздел) + кнопки «заглушить» под нуджем.

    Без snooze уведомление нечем выключить: оно повторяется каждые 6 часов, и
    единственной альтернативой было отключить бота целиком. Периоды — из
    brain.SNOOZE_PRESETS (один источник правды с обработчиком колбэка).
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import SnoozeCb
    from services.organism import brain

    kb = InlineKeyboardBuilder()
    ob = _open_button(action)
    if ob:
        kb.button(text=ob[0], web_app=ob[1])
    for code, label, _secs in brain.SNOOZE_PRESETS:
        kb.button(text=f"🔕 {label}",
                  callback_data=SnoozeCb(action="mute", sid=suggestion_id, code=code))
    kb.button(text="🚫 Больше не напоминать",
              callback_data=SnoozeCb(action="off", sid=suggestion_id, code="never"))
    # Открыть (если есть) — отдельной строкой сверху, затем snooze-периоды 2×2.
    kb.adjust(*([1, 2, 2, 1] if ob else [2, 2, 1]))
    return kb.as_markup()


async def _tick_owner(pool, bot, owner_id: int, *, notifier=None, now: float | None = None) -> bool:
    """Проверить одного владельца и при необходимости толкнуть. notifier/now —
    seam для тестов. Возвращает True, если отправили нудж."""
    from services.organism import brain, spine
    now = time.time() if now is None else now
    pulse = await brain.pulse(pool, owner_id)
    top = next((s for s in pulse.get("suggestions", []) if s["severity"] in _NUDGE_SEV), None)
    if not top:
        return False
    last = await spine.state_get(pool, owner_id, "last_nudge", {}) or {}
    last_at = float(last.get("at") or 0)
    # антиспам — только если нудж уже был: та же подсказка не чаще 6ч; любая — 2ч.
    if last_at > 0:
        if last.get("id") == top["id"] and now - last_at < _SAME_COOLDOWN:
            return False
        if now - last_at < _ANY_GAP:
            return False
    msg = f"🫀 <b>{top['title']}</b>\n{top['why']}"
    try:
        if notifier:
            await notifier(owner_id, msg)
        else:
            await bot.send_message(owner_id, msg, parse_mode="HTML",
                                   reply_markup=_snooze_kb(top["id"], top.get("action")))
    except Exception:
        log.debug("organism.runner: notify failed owner=%s", owner_id)
        return False
    await spine.state_set(pool, owner_id, "last_nudge", {"id": top["id"], "at": now})
    return True
