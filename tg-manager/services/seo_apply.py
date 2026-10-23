"""Применение AI SEO-предложений к каналам (общий слой).

Замыкает петлю анализ→рекомендация→ПРИМЕНЕНИЕ: берёт хранимое предложение из
seo_ai_suggestions (title/about/username), резолвит управляющий аккаунт канала
через managed_channels.acc_id и применяет правки через существующие функции
account_manager.edit_channel_title / edit_channel_about / set_channel_username.

ОДНА реализация переиспользуется и инлайн-эндпоинтом (одиночное применение), и
op_worker-исполнителем (массовое по всей сетке) — без дублирования логики.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_APPLY_TIMEOUT = 45  # сек на одну правку канала (инлайн-коннект к Telegram)
_VALID_FIELDS = ("title", "about", "username")


async def apply_seo_to_channel(
    pool, owner_id: int, chan_id: int, fields: list[str] | None = None
) -> dict:
    """Применить последнее SEO-предложение к одному каналу.

    Возвращает dict: {ok, applied:{field:value}, errors:{field:reason}, partial}
    либо {ok:False, error:str}. Скоуп по owner_id — чужие каналы недоступны.
    """
    want = [f for f in (fields or list(_VALID_FIELDS)) if f in _VALID_FIELDS]
    if not want:
        return {"ok": False, "error": "Нечего применять"}

    sug = await pool.fetchrow(
        "SELECT title, about, username FROM seo_ai_suggestions "
        "WHERE owner_id=$1 AND chan_id=$2 ORDER BY created_at DESC LIMIT 1",
        owner_id, chan_id)
    if not sug:
        return {"ok": False, "error": "Нет SEO-предложения для канала"}

    ch = await pool.fetchrow(
        "SELECT acc_id FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
        owner_id, chan_id)
    if not ch or not ch.get("acc_id"):
        return {"ok": False, "error": "Канал не найден или без управляющего аккаунта"}

    from database import db as _db
    from services import account_manager

    acc = await _db.get_account_for_telethon(pool, int(ch["acc_id"]), owner_id)
    if not acc or not acc.get("session_str"):
        return {"ok": False, "error": "Сессия управляющего аккаунта недоступна"}
    session = acc["session_str"]
    _acc = dict(acc)

    applied: dict = {}
    errors: dict = {}
    try:
        if "title" in want and (sug.get("title") or "").strip():
            val = sug["title"].strip()
            ok = await asyncio.wait_for(
                account_manager.edit_channel_title(session, chan_id, val, _acc=_acc),
                timeout=_APPLY_TIMEOUT)
            (applied if ok else errors)["title"] = val if ok else "не удалось"
        if "about" in want and (sug.get("about") or "").strip():
            ok = await asyncio.wait_for(
                account_manager.edit_channel_about(session, chan_id, sug["about"].strip(), _acc=_acc),
                timeout=_APPLY_TIMEOUT)
            (applied if ok else errors)["about"] = "ok" if ok else "не удалось"
        if "username" in want and (sug.get("username") or "").strip():
            val = sug["username"].strip()
            err = await asyncio.wait_for(
                account_manager.set_channel_username(session, chan_id, val, _acc=_acc),
                timeout=_APPLY_TIMEOUT)
            if err:
                errors["username"] = err
            else:
                applied["username"] = val
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Аккаунт не ответил за 45с — проверьте прокси/сессию",
                "applied": applied, "errors": errors}
    except Exception as e:  # pragma: no cover - защитный слой
        log.warning("apply_seo_to_channel owner=%s chan=%s: %s", owner_id, chan_id, e)
        return {"ok": False, "error": str(e)[:200], "applied": applied, "errors": errors}

    if applied and not errors:
        return {"ok": True, "applied": applied}
    if applied and errors:
        return {"ok": True, "applied": applied, "errors": errors, "partial": True}
    return {"ok": False, "error": "Не удалось применить",
            "errors": errors or {"_": "нет данных для применения"}}
