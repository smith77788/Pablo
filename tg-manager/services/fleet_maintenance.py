"""Единое обслуживание флота: проверить и очистить мёртвое по ВСЕМ типам разом.

«Сделать всё сразу»: один проход веером (asyncio.gather) идёт по ботам,
аккаунтам и каналам ПАРАЛЛЕЛЬНО и возвращает единую сводку — вместо того
чтобы ходить по трём экранам по очереди.

Что считается «мёртвым» берётся из ЕДИНЫХ словарей, а не выдумывается заново:
  • бот мёртв — токен отозван (getMe → 401); решение целиком в
    bot_api.check_alive, здесь только его зовём;
  • аккаунт удаляем только БЕЗВОЗВРАТНО потерянный — account_status.LOST_STATUSES
    (banned/deactivated/deleted). spamblock/frozen (снимаются) и
    session_expired (сессия переподключается) НЕ трогаем: это восстановимое, и
    выбрасывать его нельзя. Словарь один на продукт (test_dead_status_has_one_door
    запрещает копии), поэтому берём его, а не свой список;
  • у каналов живой проверки доступности пока нет (нужна сессия управляющего
    аккаунта — отдельная операция движка), поэтому их только считаем и честно
    помечаем, что проверка не выполнялась.

Очистка действует в Infragram и ограничена владельцем. У ботов сохраняются
аудитория и история. Подтверждённо потерянные аккаунты удаляются из базы.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def _deleted_count(res) -> int:
    """Число строк из тега asyncpg «DELETE N» (0 при ином ответе)."""
    s = str(res)
    if not s.startswith("DELETE"):
        return 0
    try:
        return int(s.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def _scan_bots(pool, uid: int) -> tuple[dict, dict[int, str]]:
    """Вернуть сводку и снимок отозванных токенов для сравнения при записи."""
    rows = await pool.fetch(
        "SELECT bot_id, token FROM managed_bots WHERE added_by=$1 "
        "AND deleted_at IS NULL AND NOT cleanup_paused ORDER BY bot_id", uid)
    out = {"total": len(rows), "alive": 0, "dead": 0, "unknown": 0, "dead_ids": []}
    if not rows:
        return out, {}
    import aiohttp

    from services import bot_api
    tokens = [r["token"] for r in rows]
    async with aiohttp.ClientSession() as sess:
        statuses = await bot_api.batch_check_alive(sess, tokens)
    if len(rows) != len(statuses):
        raise ValueError("Проверка вернула неполный результат")
    checked_tokens = {row["bot_id"]: row["token"] for row, status in zip(rows, statuses)
                      if status == bot_api.BOT_DEAD}
    out.update(alive=statuses.count(bot_api.BOT_ALIVE), unknown=statuses.count(bot_api.BOT_UNKNOWN),
               dead=len(checked_tokens), dead_ids=list(checked_tokens))
    return out, checked_tokens


async def _scan_accounts(pool, uid: int) -> dict:
    """Безвозвратно потерянные аккаунты владельца (account_status.LOST_STATUSES).

    Только LOST (banned/deactivated/deleted) — «потерян и сам не вернётся».
    Восстановимое (spamblock/frozen/session_expired) не трогаем. Подключения
    нет — судим по сохранённому acc_status (его ставит проверка аккаунтов).
    """
    from services import account_status
    rows = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 "
        "AND COALESCE(acc_status,'active') = ANY($2::text[])",
        uid, list(account_status.LOST_STATUSES))
    dead_ids = [int(r["id"]) for r in (rows or [])]
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", uid)
    return {"total": int(total or 0), "dead": len(dead_ids), "dead_ids": dead_ids}


async def _scan_channels(pool, uid: int) -> dict:
    """Каналов под управлением — счёт. Живой проверки доступности пока нет."""
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
    return {"total": int(total or 0), "dead": 0, "dead_ids": [],
            "note": "проверка доступности каналов ещё не реализована"}


async def sweep_fleet(pool, uid: int, *, delete: bool, preview: bool) -> dict:
    """Единый проход по флоту.

    delete=False      — только проверка: вернуть счётчики, ничего не трогать.
    delete=True,      — удалить мёртвое; preview=True сперва только считает
    preview=True/False  (ничего не удаляя), preview=False — удаляет.

    Боты, аккаунты и каналы сканируются параллельно. Исключение одной ветки не
    валит весь проход — она отдаёт ошибку в своей части сводки, остальные идут.
    """
    bots, accounts, channels = await asyncio.gather(
        _scan_bots(pool, uid), _scan_accounts(pool, uid), _scan_channels(pool, uid),
        return_exceptions=True,
    )
    if isinstance(bots, Exception):
        # Исключение Bot API может включать URL с токеном.
        log.warning("sweep_fleet bots uid=%d error=%s", uid, type(bots).__name__)
        bot_summary, checked_tokens = {"error": "Не удалось проверить ботов"}, {}
    else:
        bot_summary, checked_tokens = bots
    result = {
        "ok": True,
        "deleted": bool(delete and not preview),
        "preview": bool(delete and preview),
        "bots": bot_summary,
        "accounts": accounts if not isinstance(accounts, Exception) else {"error": str(accounts)},
        "channels": channels if not isinstance(channels, Exception) else {"error": str(channels)},
    }
    if isinstance(accounts, Exception):
        log.exception("sweep_fleet accounts uid=%d", uid, exc_info=accounts)

    acc_ids = result["accounts"].get("dead_ids", []) if isinstance(accounts, dict) else []
    result["dead_total"] = len(checked_tokens) + len(acc_ids)

    if not delete or preview:
        result["deleted_total"] = 0
        return result

    async def _del_bots() -> int:
        if not checked_tokens:
            return 0
        from database.db import delete_bots

        removed = await delete_bots(pool, uid, list(checked_tokens), checked_tokens=checked_tokens)
        return len(removed)

    async def _del_accounts() -> int:
        if not acc_ids:
            return 0
        res = await pool.execute(
            "DELETE FROM tg_accounts WHERE id = ANY($1::bigint[]) AND owner_id=$2",
            acc_ids, uid)
        return _deleted_count(res)

    bots_del, accs_del = await asyncio.gather(_del_bots(), _del_accounts())
    result["bots"]["deleted"] = bots_del
    result["bots"]["changed_since_check"] = len(checked_tokens) - bots_del
    result["accounts"]["deleted"] = accs_del
    result["deleted_total"] = bots_del + accs_del

    try:
        from database.db import record_manual_action
        await record_manual_action(
            pool, uid, "fleet_sweep_cleanup",
            target=f"bots={bots_del},accounts={accs_del}")
    except Exception:
        log.exception("sweep_fleet audit uid=%d", uid)
    return result
