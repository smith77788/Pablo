"""Массовое включение режима bot-to-bot у управляемых ботов через @BotFather.

Bot Mesh (services/bot_mesh.py) требует, чтобы боты-участники имели включённый
Bot-to-Bot Communication Mode. Включается он ТОЛЬКО в @BotFather аккаунтом-
владельцем бота — API для этого нет. Поэтому массовое включение = автоматический
диалог с BotFather от каждого аккаунта-владельца (managed_bots.acc_id).

Разбор ответов BotFather — чистая функция `find_b2b_toggle` (её и проверяют
тесты). Сама навигация по меню на живом Telegram юнит-тестами непокрываема
(заглушка типы не связывает) и вынесена в один сив `enable_b2b_via_botfather`.

ЧЕСТНАЯ ГРАНИЦА: точные подписи пунктов меню BotFather для этого режима
локале-зависимы и не проверены на живой сессии. Матчер ищет по набору
кандидатов (англ./рус.) и, если пункт не найден, возвращает «не найдено» —
не притворяется, что включил. Первый прогон — канарейкой на 1–2 ботах
(см. CLAUDE.md: массовая операция начинается с канарейки).
"""
from __future__ import annotations

import asyncio
import logging
import random

log = logging.getLogger(__name__)

_BOTFATHER = "BotFather"
_CONNECT_TIMEOUT = 30
# Потолок на ОТДЕЛЬНЫЙ запрос к Telegram. Коннект ограничен своим таймаутом,
# но мёртвый прокси чаще отдаёт не отказ, а half-open сокет: TCP установлен,
# ответа нет и не будет, и запрос не возвращается никогда — операция стоит,
# держа слот и арендованный флот. Значение щедрое намеренно: живой Telegram
# отвечает за секунды, ложный таймаут увёл бы цель в лишний повтор.
_ACTION_TIMEOUT = 45


# Кандидаты подписи пункта «Bot-to-Bot» в меню бота у BotFather (низкий регистр).
_B2B_KEYS = ("bot-to-bot", "bot to bot", "b2b", "бот-бот", "бот к боту",
             "botmode", "bot-bot")
# Кандидаты входа в настройки бота, где живёт переключатель.
_SETTINGS_KEYS = ("bot settings", "настройки бота", "settings")
# Признаки, что режим УЖЕ включён (кнопка предлагает выключить / стоит галочка).
_ON_MARKERS = ("turn off", "disable", "выключить", "отключить", "✅", "enabled", "on")
# Признаки, что режим выключен (кнопка предлагает включить).
_OFF_MARKERS = ("turn on", "enable", "включить", "off", "disabled")


def _labels(msg) -> list[str]:
    """Все подписи inline-кнопок сообщения, как есть (для клика) + низкий регистр."""
    out = []
    rows = getattr(getattr(msg, "reply_markup", None), "rows", None) or []
    for row in rows:
        for btn in (getattr(row, "buttons", None) or []):
            t = getattr(btn, "text", None)
            if t:
                out.append(t)
    return out


def find_button(labels: list[str], keys) -> str | None:
    """Первая кнопка, чья подпись содержит любой из ключей. Возвращает ТОЧНУЮ
    подпись (для клика) или None."""
    for t in labels:
        low = str(t).strip().lower()
        if any(k in low for k in keys):
            return t
    return None


def find_b2b_toggle(labels: list[str]) -> dict:
    """Разобрать меню бота: есть ли пункт bot-to-bot и включён ли он.

    Возвращает {"button": <точная подпись|None>, "state": on|off|unknown}.
    state='on' — режим уже включён (кнопка выключает); 'off' — выключен
    (кнопка включает); 'unknown' — пункт есть, но состояние по подписи не ясно.
    """
    btn = find_button(labels, _B2B_KEYS)
    if btn is None:
        return {"button": None, "state": "unknown"}
    low = btn.strip().lower()
    # Подпись «Turn off …» означает, что СЕЙЧАС включено, и наоборот.
    if any(m in low for m in ("turn off", "disable", "выключить", "отключить")):
        return {"button": btn, "state": "on"}
    if any(m in low for m in ("turn on", "enable", "включить")):
        return {"button": btn, "state": "off"}
    if "✅" in btn:
        return {"button": btn, "state": "on"}
    return {"button": btn, "state": "unknown"}


def plan_targets(bots: list[dict], *, only_disabled: bool = True,
                 limit: int = 50) -> dict[int, list[str]]:
    """Сгруппировать ботов по аккаунту-владельцу (acc_id) → список @username.

    Боты без acc_id включить нельзя (нет сессии-владельца) — они отсеиваются
    вызывающим по ключу None. only_disabled: пропускать уже включённые.
    """
    out: dict[int, list[str]] = {}
    n = 0
    for b in bots or []:
        if only_disabled and b.get("b2b_enabled"):
            continue
        acc = b.get("acc_id")
        uname = (b.get("username") or "").lstrip("@")
        if not uname:
            continue
        out.setdefault(acc, []).append(uname)
        n += 1
        if n >= max(1, int(limit)):
            break
    return out


async def enable_b2b_via_botfather(session_string: str, usernames: list,
                                   _acc: dict | None = None,
                                   limit: int = 20) -> dict:
    """Включить bot-to-bot у указанных ботов через @BotFather (одна сессия).

    Возвращает {'enabled': [uname…], 'already': [uname…], 'errors': {uname: why}}.
    Диалог платный по лимитам: паузы между ботами, потолок на прогон.
    """
    from services.account_manager import _make_client

    enabled: list = []
    already: list = []
    errors: dict = {}
    wanted = [str(u).lstrip("@") for u in (usernames or []) if str(u or "").strip()]
    wanted = wanted[:max(1, int(limit))]
    if not wanted:
        return {"enabled": [], "already": [], "errors": {}}

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        bf = await asyncio.wait_for(client.get_entity(_BOTFATHER), timeout=_ACTION_TIMEOUT)

        async def _latest():
            msgs = await asyncio.wait_for(client.get_messages(bf, limit=1), timeout=_ACTION_TIMEOUT)
            return msgs[0] if msgs else None

        for uname in wanted:
            try:
                await asyncio.sleep(random.uniform(2.0, 4.0))
                await asyncio.wait_for(client.send_message(bf, "/mybots"), timeout=_ACTION_TIMEOUT)
                await asyncio.sleep(3.0)
                msg = await _latest()
                if msg is None:
                    errors[uname] = "BotFather не ответил"
                    continue
                try:
                    await msg.click(text=f"@{uname}")
                except Exception:
                    errors[uname] = "бот не найден в списке BotFather"
                    continue
                await asyncio.sleep(2.5)
                msg = await _latest()
                # Войти в настройки, если пункт есть (иначе ищем toggle прямо тут).
                st_btn = find_button(_labels(msg), _SETTINGS_KEYS)
                if st_btn:
                    try:
                        await msg.click(text=st_btn)
                        await asyncio.sleep(2.5)
                        msg = await _latest()
                    except Exception:
                        pass
                toggle = find_b2b_toggle(_labels(msg))
                if toggle["button"] is None:
                    errors[uname] = "пункт bot-to-bot не найден (проверьте вручную)"
                    continue
                if toggle["state"] == "on":
                    already.append(uname)
                    continue
                try:
                    await msg.click(text=toggle["button"])
                    await asyncio.sleep(2.0)
                    enabled.append(uname)
                except Exception as e:
                    errors[uname] = f"не удалось нажать: {str(e)[:80]}"
            except Exception as e:
                errors[uname] = str(e)[:120]
        return {"enabled": enabled, "already": already, "errors": errors}
    except Exception as e:
        low = str(e).lower()
        why = ("сессия недействительна"
               if any(x in low for x in ("auth", "unauthorized", "key is not registered"))
               else str(e)[:160])
        return {"enabled": enabled, "already": already,
                "errors": {**errors, "_": why}}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("enable_b2b_via_botfather: disconnect failed")
