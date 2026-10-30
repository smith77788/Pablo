"""Флот — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: пять маршрутов `/api/miniapp/fleet*` —
диагностика «почему флот не исполняет», разбор отсеявшихся на шаге воронки
аккаунтов, единое обслуживание флота, состояние губернатора темпа и мягкие
предупреждения перед массовой операцией.

Семейство выбрано для выноса как связное: все пять ручек читают состояние
ФЛОТА и делегируют работу сервисам (`fleet_doctor`, `fleet_maintenance`,
`fleet_governor`, `infra_orchestrator`), своей бизнес-логики не держат и с
остальным монолитом делят только общие помощники ответа.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно. `fleet_warnings` лежала в монолите в тысяче
строк от остальных четырёх — здесь она рядом с ними, и это единственное, что
изменилось в её окружении.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты флота на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _safe_fetch,
        log,
    )
    from services.security import validate_string

    async def fleet_diagnose(request: web.Request) -> web.Response:
        """Диагностика «почему флот не исполняет»: воронка отбора аккаунтов +
        конфиг + залипшие операции + последние реальные ошибки."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import fleet_doctor
            return _json_resp(await fleet_doctor.diagnose(pool, uid))
        except Exception:
            log.exception("fleet_diagnose uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def fleet_diagnose_blocked(request: web.Request) -> web.Response:
        """Какие именно аккаунты отсеялись на шаге воронки — и чем они больны.

        Вердикт «Ни у одного аккаунта нет сессии» называет болезнь, но не
        больных: выяснить, какие это аккаунты, экран не позволял, и чинить
        приходилось наугад по всему флоту.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import fleet_doctor
        step = validate_string(request.query.get("step"), 32, required=False) or ""
        if step not in fleet_doctor.FUNNEL_STEPS:
            return _err("Неизвестный шаг воронки", 400)
        limit = _list_limit(request, 100, 300)
        rows = await _safe_fetch(
            pool,
            "SELECT id, phone, first_name, username, is_active, "
            "(session_str IS NOT NULL AND session_str <> '') AS has_session, "
            "COALESCE(acc_status,'active') AS acc_status, "
            "(cooldown_until IS NOT NULL AND cooldown_until > NOW()) AS cd_active, "
            "trust_score "
            "FROM tg_accounts WHERE owner_id=$1 ORDER BY id",
            uid,
        )
        blocked = fleet_doctor.step_blockers([dict(r) for r in rows], step)
        items = [{
            "id": b.get("id"),
            "label": (b.get("first_name") or (("@" + b["username"]) if b.get("username") else None)
                      or b.get("phone") or f"Аккаунт {b.get('id')}"),
            "reason": b.get("reason", ""),
        } for b in blocked[:limit]]
        return _json_resp({"step": step, "items": items, "total": len(blocked)})

    async def fleet_sweep(request: web.Request) -> web.Response:
        """Единое обслуживание флота «сделать всё разом»: параллельно по ботам,
        аккаунтам и каналам — проверить живость и (по желанию) удалить мёртвое.

        body: {delete: bool, preview: bool}. delete=false — только проверка;
        delete=true,preview=true — посчитать, что удалится (без удаления);
        delete=true,preview=false — удалить мёртвое. Удаляется только то, что
        ТОЧНО мёртвое (бот с отозванным токеном, аккаунт с терминальным
        статусом); «не ответившие» не трогаются.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        want_delete = bool(body.get("delete"))
        preview = bool(body.get("preview"))
        try:
            from services import fleet_maintenance
            return _json_resp(await fleet_maintenance.sweep_fleet(
                pool, uid, delete=want_delete, preview=preview))
        except Exception:
            log.exception("fleet_sweep uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def fleet_governor_status(request: web.Request) -> web.Response:
        """Состояние глобального губернатора темпа: уровень, множитель, причина."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import fleet_governor
            return _json_resp(await fleet_governor.status(pool, uid))
        except Exception:
            log.exception("fleet_governor_status uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def fleet_warnings(request: web.Request) -> web.Response:
        """Мягкие предупреждения перед массовой операцией.

        Оба предупреждения давно существуют, но показывались ТОЛЬКО в боте:
        основной интерфейс — мини-апп — запускал массовые операции вслепую.
        Fail-open: предупреждения не должны мешать работать.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        warnings = []
        try:
            from services import infra_orchestrator as _io
            for fn in (_io.get_pressure_warning, _io.get_readiness_warning,
                       _io.get_dead_proxy_warning):
                try:
                    w = await fn(pool, uid)
                except Exception:
                    w = None
                if w:
                    warnings.append(w)
        except Exception:
            log.debug("fleet_warnings uid=%s", uid, exc_info=True)
        return _json_resp({"warnings": warnings})

    app.router.add_get("/api/miniapp/fleet/governor", fleet_governor_status)
    app.router.add_get("/api/miniapp/fleet/diagnose", fleet_diagnose)
    app.router.add_get("/api/miniapp/fleet/diagnose/blocked", fleet_diagnose_blocked)
    app.router.add_post("/api/miniapp/fleet/sweep", fleet_sweep)
    app.router.add_get("/api/miniapp/fleet/warnings", fleet_warnings)
