"""Авто-мониторинг флота — фоновый «демон» периодических проверок.

Шаг к Telegram-ОС: у продукта есть разовые read-only проверки (здоровье
аккаунтов, ограничения каналов/чатов/ботов, гео-доступность по странам), но запускались они
только руками. Здесь владелец включает их как ПЕРИОДИЧЕСКИЕ: «проверяй флот сам
каждые N часов». Находки идут в общий пульс (restriction_events), как и у
разовых запусков.

Реализация переиспользует уже проверенную рекуррентную инфраструктуру
op_worker: операция ставится через шину с `params.repeat_interval_min`, и после
каждого круга op_worker сам ставит следующий (allowlist `_RECURRING_OK_OPS`,
предохранитель Ban Weather, гейт тарифа и счётчик неудач подряд — на месте).
Второго планировщика не заводим.

Включение = поставить рекуррентную операцию этого типа (сняв прежнюю, чтобы не
плодить параллельные круги). Выключение = отменить активный круг. Статус =
есть ли активный круг (pending/scheduled/running/paused с repeat_interval_min>0).
"""
from __future__ import annotations

from aiohttp import web

# Вид мониторинга → тип операции (read-only исполнители в op_worker).
_MONITOR_OPS = {
    "health": "check_accounts_health",
    "restrictions": "check_owned_restrictions",
    "geoban": "check_bot_geoban",
}
_MONITOR_LABELS = {
    "health": "Здоровье аккаунтов",
    "restrictions": "Ограничения и теневой бан",
    "geoban": "Гео-доступность каналов, чатов и ботов",
}

# Интервал повтора: не чаще раза в час (read-only, но всё же нагрузка на флот и
# запросы к Telegram), не реже раза в месяц. По умолчанию — дважды в сутки.
_MIN_INTERVAL_MIN = 60
_MAX_INTERVAL_MIN = 60 * 24 * 30
_DEFAULT_INTERVAL_MIN = 12 * 60

# Статусы «круг жив» — активный рекуррентный мониторинг этого типа.
_ACTIVE_STATES = ("pending", "scheduled", "running", "paused")


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _err, _get_uid, _json_resp, log

    async def _active_recurring(uid: int, op_type: str):
        """Активный круг рекуррентной проверки этого типа у владельца, или None."""
        try:
            return await pool.fetchrow(
                """SELECT id, status, scheduled_for,
                          COALESCE((params->>'repeat_interval_min')::int, 0) AS rmin
                     FROM operation_queue
                    WHERE owner_id=$1 AND op_type=$2
                      AND status = ANY($3::text[])
                      AND COALESCE((params->>'repeat_interval_min')::int, 0) > 0
                    ORDER BY id DESC LIMIT 1""",
                uid, op_type, list(_ACTIVE_STATES))
        except Exception:
            log.debug("monitor: _active_recurring query failed")
            return None

    async def monitor_status(request: web.Request) -> web.Response:
        """Состояние авто-мониторинга по каждому виду проверки."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        monitors: dict = {}
        for kind, op_type in _MONITOR_OPS.items():
            row = await _active_recurring(uid, op_type)
            monitors[kind] = {
                "label": _MONITOR_LABELS[kind],
                "enabled": bool(row),
                "interval_min": int(row["rmin"]) if row else None,
                "next_run_at": (row["scheduled_for"].isoformat()
                                if row and row.get("scheduled_for") else None),
                "op_id": int(row["id"]) if row else None,
            }
        return _json_resp({"ok": True, "monitors": monitors,
                           "default_interval_min": _DEFAULT_INTERVAL_MIN})

    async def monitor_toggle(request: web.Request) -> web.Response:
        """Включить/выключить периодическую проверку указанного вида.

        Включение сначала СНИМАЕТ прежний круг этого типа (чтобы не плодить
        параллельные расписания), затем ставит новый с repeat_interval_min.
        Выключение — отменяет активный круг.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        kind = (body.get("kind") or "").strip()
        op_type = _MONITOR_OPS.get(kind)
        if not op_type:
            return _err("Неизвестный вид проверки", 400)
        enabled = bool(body.get("enabled"))

        from services import operation_bus as _obus

        # Снимаем прежний круг этого типа в любом случае (дедуп при включении,
        # остановка при выключении).
        existing = await _active_recurring(uid, op_type)
        if existing:
            try:
                await _obus.cancel(pool, int(existing["id"]), uid, allow=_ACTIVE_STATES)
            except Exception:
                log.debug("monitor: cancel previous recurring failed")

        if not enabled:
            return _json_resp({"ok": True, "kind": kind, "enabled": False})

        try:
            interval = int(body.get("interval_min") or _DEFAULT_INTERVAL_MIN)
        except (TypeError, ValueError):
            interval = _DEFAULT_INTERVAL_MIN
        interval = max(_MIN_INTERVAL_MIN, min(interval, _MAX_INTERVAL_MIN))

        # total_items исполнитель проставит сам в начале прогона (он делает
        # UPDATE operation_queue SET total_items=...). Первый круг стартует сразу
        # (scheduled_for не задаём) — владелец видит, что мониторинг работает.
        params = {"owner_id": uid, "repeat_interval_min": interval, "monitor": True}
        try:
            op_id = await _obus.submit(pool, uid, op_type, params, total_items=0)
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("monitor_toggle submit uid=%d kind=%s", uid, kind)
            return _err("Не удалось включить мониторинг", 500)
        return _json_resp({"ok": True, "kind": kind, "enabled": True,
                           "interval_min": interval, "op_id": op_id})

    app.router.add_get("/api/miniapp/monitor/status", monitor_status)
    app.router.add_post("/api/miniapp/monitor/toggle", monitor_toggle)
