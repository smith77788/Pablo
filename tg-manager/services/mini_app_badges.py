"""Живые счётчики на вкладках мини-аппа: одна ручка на весь таббар.

ЗАЧЕМ. Нижние вкладки были статичны: ни «Итоги», ни «Аккаунты», ни
«Сообщения» не говорили, что там что-то изменилось. Чтобы узнать, не
ограничили ли аккаунт и не идёт ли рассылка, владелец открывал экраны по
очереди — а единственное, что он хочет знать, зайдя в приложение: есть ли
где-то то, с чем надо что-то делать. Значок на вкладке отвечает на это сразу.

ОДНА РУЧКА, А НЕ ТРИ. Значки обновляются вместе и часто (раз в минуту, плюс
при возврате в приложение), поэтому три запроса с экрана означали бы три
круга по сети на каждое обновление. Здесь один ответ на все вкладки.

КАЖДЫЙ СЧЁТЧИК ФЕЙЛИТСЯ ОТДЕЛЬНО И ЗАКРЫТО В НОЛЬ. Значок — украшение: из-за
упавшего счётчика рассылок не должен пропадать счётчик уведомлений, и ни один
из них не должен ронять ответ. Ноль в значке означает «нечего показать», и это
честнее выдуманного числа: соврать «3 аккаунта в беде» хуже, чем промолчать.

СКОУП ДЕРЖИТ ЗАПРОС. owner_id/created_by стоят в самих условиях, а не в
обработчике: без этого значок показывал бы владельцу чужую беду.
"""
from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)

# Аккаунты, с которыми надо что-то делать: заблокированные, в спамблоке и
# простаивающие из-за мёртвого прокси (сам аккаунт цел — чинить надо прокси).
# Кулдаун сюда НЕ входит: он проходит сам, и значок о нём — ложная тревога.
_ACCOUNTS_SQL = """
SELECT COUNT(*) FROM tg_accounts
 WHERE owner_id = $1
   AND (COALESCE(acc_status,'ok') IN ('banned','spamblock','deactivated','session_expired')
        OR (proxy_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM user_proxies p
               WHERE p.id = tg_accounts.proxy_id AND p.is_alive IS FALSE)))
"""

# Рассылки в работе: 'pending' поднимает докатка при старте процесса, 'running'
# идёт прямо сейчас — и то и другое означает «сообщения ещё уходят».
_BROADCASTS_SQL = """
SELECT COUNT(*) FROM broadcasts
 WHERE created_by = $1 AND lower(status) IN ('pending','running')
"""

_NOTIFICATIONS_SQL = """
SELECT COUNT(*) FROM notification_feed WHERE owner_id = $1 AND read_at IS NULL
"""

# Операции в работе: их значок висит на «Разделах», где лежит экран операций.
_OPERATIONS_SQL = """
SELECT COUNT(*) FROM operation_queue
 WHERE owner_id = $1 AND status IN ('pending','running')
"""

_COUNTS: dict[str, str] = {
    "notifications": _NOTIFICATIONS_SQL,
    "accounts": _ACCOUNTS_SQL,
    "broadcasts": _BROADCASTS_SQL,
    "operations": _OPERATIONS_SQL,
}


async def collect(pool, uid: int) -> dict[str, int]:
    """Счётчики для таббара. Упавший счётчик — ноль, а не упавший ответ."""
    out: dict[str, int] = {}
    for key, sql in _COUNTS.items():
        try:
            out[key] = int(await pool.fetchval(sql, uid) or 0)
        except Exception as exc:
            log.debug("badges: счётчик %s не прочитан uid=%s: %s", key, uid, str(exc)[:120])
            out[key] = 0
    return out


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _err, _get_uid, _json_resp

    async def badges(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        return _json_resp(await collect(pool, uid))

    app.router.add_get("/api/miniapp/badges", badges)
