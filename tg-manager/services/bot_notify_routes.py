"""Каналы-приёмники уведомлений о событиях ботов (schema_v384).

Владелец заводит сколько угодно маршрутов — по одному на сеть проектов:
КУДА (канал/группа, где главный бот админ), КАКИЕ боты (все или выбранные;
id 0 — каналы/чаты, где админ сам главный бот) и КАКИЕ события («запустили
бота», «заявка на вступление»). Событие уходит во ВСЕ подходящие маршруты.

Личка владельца остаётся страховкой: она замолкает, только если подходящий
маршрут с «не дублировать в личку» действительно доставил сообщение. Канал,
откуда бота выгнали, не съедает уведомление — оно придёт в личку, а у маршрута
появится отметка об ошибке.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

EVENTS = ("start", "join_request")
EVENT_LABELS = {"start": "Запуск бота (новые и вернувшиеся)",
                "join_request": "Заявки на вступление"}
MAX_ROUTES = 50
NAME_MAX = 60


def _as_list(v: Any) -> list | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return list(v)


def matches(route: dict, bot_id: int, event: str) -> bool:
    """Подходит ли маршрут событию. ЧИСТАЯ."""
    if not route.get("enabled", True):
        return False
    events = _as_list(route.get("events")) or list(EVENTS)
    if event not in events:
        return False
    bots = _as_list(route.get("bot_ids"))
    return bots is None or int(bot_id) in {int(b) for b in bots}


def normalize(body: dict) -> tuple[dict | None, str]:
    """Проверить маршрут с экрана. (маршрут, '') или (None, ошибка). ЧИСТАЯ."""
    try:
        chat_id = int(body.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if not chat_id:
        return None, "Выберите канал, куда слать уведомления"
    events = [e for e in (body.get("events") or []) if e in EVENTS]
    if not events:
        return None, "Отметьте хотя бы одно событие"
    raw_bots = body.get("bot_ids")
    bot_ids = None
    if raw_bots is not None:
        try:
            bot_ids = sorted({int(b) for b in raw_bots})
        except (TypeError, ValueError):
            return None, "Неверный список ботов"
        if not bot_ids:
            return None, "Выберите хотя бы одного бота или «все боты»"
    name = str(body.get("name") or "").strip()[:NAME_MAX]
    return {
        "name": name, "chat_id": chat_id, "bot_ids": bot_ids, "events": events,
        "mute_dm": bool(body.get("mute_dm")), "enabled": body.get("enabled", True) is not False,
    }, ""


async def list_routes(pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, name, chat_id, chat_title, bot_ids, events, mute_dm, enabled, "
        "last_error, updated_at FROM bot_notify_routes WHERE owner_id=$1 ORDER BY id",
        owner_id)
    out = []
    for r in rows or ():
        d = dict(r)
        d["bot_ids"] = _as_list(d.get("bot_ids"))
        d["events"] = _as_list(d.get("events")) or []
        out.append(d)
    return out


async def save_route(pool, owner_id: int, route: dict, chat_title: str,
                     route_id: int | None = None) -> int | None:
    """Создать или обновить маршрут владельца. Возвращает id (None — нет такого)."""
    if route_id:
        return await pool.fetchval(
            """UPDATE bot_notify_routes
                  SET name=$3, chat_id=$4, chat_title=$5, bot_ids=$6::bigint[],
                      events=$7::text[], mute_dm=$8, enabled=$9, last_error=NULL,
                      updated_at=now()
                WHERE id=$1 AND owner_id=$2 RETURNING id""",
            int(route_id), owner_id, route["name"], route["chat_id"], chat_title,
            route["bot_ids"], route["events"], route["mute_dm"], route["enabled"])
    n = await pool.fetchval(
        "SELECT count(*) FROM bot_notify_routes WHERE owner_id=$1", owner_id)
    if int(n or 0) >= MAX_ROUTES:
        raise ValueError(f"Не больше {MAX_ROUTES} каналов уведомлений")
    return await pool.fetchval(
        """INSERT INTO bot_notify_routes(owner_id, name, chat_id, chat_title, bot_ids,
                                         events, mute_dm, enabled)
           VALUES($1,$2,$3,$4,$5::bigint[],$6::text[],$7,$8) RETURNING id""",
        owner_id, route["name"], route["chat_id"], chat_title, route["bot_ids"],
        route["events"], route["mute_dm"], route["enabled"])


async def delete_route(pool, owner_id: int, route_id: int) -> bool:
    r = await pool.execute(
        "DELETE FROM bot_notify_routes WHERE id=$1 AND owner_id=$2", int(route_id), owner_id)
    return str(r).endswith(" 1")


async def deliver(pool, main_bot, owner_id: int, *, bot_id: int, event: str,
                  text: str, reply_markup=None) -> bool:
    """Разослать по подходящим маршрутам. True — личку можно не дублировать.

    Сбой чтения маршрутов или доставки не мешает личке: вернётся False.
    """
    try:
        routes = await list_routes(pool, owner_id)
    except Exception:
        log.debug("bot_notify_routes: маршруты не прочитаны owner=%s", owner_id,
                  exc_info=True)
        return False
    mute = False
    for r in routes:
        if not matches(r, bot_id, event):
            continue
        try:
            await main_bot.send_message(int(r["chat_id"]), text, parse_mode="HTML",
                                        reply_markup=reply_markup,
                                        disable_web_page_preview=True)
            if r.get("last_error"):
                await _mark(pool, r["id"], None)
            if r.get("mute_dm"):
                mute = True
        except Exception as e:
            log.warning("bot_notify_routes: канал %s маршрута %s не принял: %s",
                        r.get("chat_id"), r.get("id"), str(e)[:160])
            await _mark(pool, r["id"], send_error_ru(e))
    return mute


async def _mark(pool, route_id: int, error: str | None) -> None:
    try:
        await pool.execute(
            "UPDATE bot_notify_routes SET last_error=$2 WHERE id=$1", route_id, error)
    except Exception:
        log.debug("bot_notify_routes: отметка ошибки", exc_info=True)


def send_error_ru(exc: BaseException) -> str:
    t = str(exc).lower()
    if "not enough rights" in t or "need administrator" in t or "have no rights" in t:
        return "У бота нет права публиковать в этом канале"
    if "chat not found" in t or "kicked" in t or "forbidden" in t:
        return "Бота убрали из канала — добавьте его админом снова"
    if "too many requests" in t or "retry after" in t:
        return "Telegram временно ограничил отправку"
    return "Канал не принял сообщение: " + str(exc)[:80]
