"""Админские ручки мини-аппа.

Вынесено из `services/mini_app_api.py`: 10 маршрутов `/api/miniapp/admin/*` —
список пользователей, сводка, карточка пользователя, рассылка от имени
сервиса, выдача и отзыв доступа, бан и разбан, журнал действий и статистика
операций.

Все десять закрыты `_is_admin` из монолита: это единственный признак
администратора в проекте, второго источника правды здесь не появляется.

Вместе с обработчиками уехало замыкание `_admin_target(request)` — разбор
`{user_id}` из пути вместе с проверкой прав, одной дверью для выдачи,
отзыва, бана и разбана. Его не зовёт больше никто.

`_admin_broadcast_core` остался на уровне монолита и импортируется:
его зовёт напрямую `tests/test_admin_actions_are_audited.py`, а рассылка
обязана писать в журнал — тест держит именно это.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать админские маршруты мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _admin_broadcast_core, _err, _get_uid, _is_admin,
        _json_resp, log,
    )


    async def admin_users(request: web.Request) -> web.Response:
        """Список пользователей платформы (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            rows = await pool.fetch(
                """SELECT user_id, username, first_name, current_plan, plan_expires_at,
                          registered_at AS created_at, last_seen AS last_active_at
                   FROM platform_users ORDER BY registered_at DESC LIMIT 100""")
            return _json_resp({"users": [dict(r) for r in rows]})
        except Exception:
            log.exception("admin_users uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_stats(request: web.Request) -> web.Response:
        """Системная статистика (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            stats = {}
            stats["total_users"] = int(await pool.fetchval("SELECT COUNT(*) FROM platform_users") or 0)
            stats["active_subs"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE is_active=true AND expires_at > now()") or 0)
            stats["total_bots"] = int(await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE is_active=true") or 0)
            stats["total_channels"] = int(await pool.fetchval("SELECT COUNT(*) FROM managed_channels") or 0)
            stats["total_accounts"] = int(await pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE is_active=true") or 0)
            stats["ops_today"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE created_at > now() - INTERVAL '1 day'") or 0)
            stats["ops_pending"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='pending'") or 0)
            stats["ops_running"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='running'") or 0)
            return _json_resp(stats)
        except Exception:
            log.exception("admin_stats uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_user_detail(request: web.Request) -> web.Response:
        """Детали пользователя (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            target_id = int(request.match_info["user_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор пользователя", 400)
        try:
            user = await pool.fetchrow(
                """SELECT *, registered_at AS created_at, last_seen AS last_active_at
                   FROM platform_users WHERE user_id=$1""", target_id)
            if not user:
                return _err("Пользователь не найден", 404)
            bots = await pool.fetchval(
                "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=true", target_id) or 0
            channels = await pool.fetchval(
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", target_id) or 0
            accounts = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true", target_id) or 0
            return _json_resp({
                "user": dict(user),
                "bots": int(bots),
                "channels": int(channels),
                "accounts": int(accounts),
            })
        except Exception:
            log.exception("admin_user_detail uid=%d target=%d", uid, target_id)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_broadcast(request: web.Request) -> web.Response:
        """Рассылка всем пользователям (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        text = (body.get("text") or "").strip()
        if not text:
            return _err("Нужен текст")
        try:
            return _json_resp(await _admin_broadcast_core(pool, uid, text))
        except Exception:
            log.exception("admin_broadcast uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _admin_target(request: web.Request):
        """Общая проверка админ-действия: (uid, target_id) или (None, error-resp)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return None, None, _err("Раздел только для владельца платформы", 403)
        try:
            target_id = int(request.match_info["user_id"])
        except (KeyError, ValueError):
            return None, None, _err("Неверный идентификатор пользователя", 400)
        return uid, target_id, None

    async def admin_user_grant(request: web.Request) -> web.Response:
        """Выдать подписку пользователю (paid) на N месяцев."""
        uid, target_id, err = await _admin_target(request)
        # `err is not None`, а НЕ `if err`: aiohttp StreamResponse определяет
        # __len__, поэтому у ответа с пустым на этот момент телом bool() == False.
        # Проверка молча не срабатывала — 403 создавался и выбрасывался, а
        # выполнение шло дальше как у администратора.
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            months = int(body.get("months") or 1)
        except (TypeError, ValueError):
            months = 1
        months = max(1, min(months, 120))
        try:
            from database import db as _db
            await _db.grant_plan_to_user(pool, target_id, uid, "paid", months)
            return _json_resp({"ok": True, "months": months})
        except Exception:
            log.exception("admin_user_grant uid=%d target=%d", uid, target_id)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_user_revoke(request: web.Request) -> web.Response:
        """Отозвать подписку пользователя."""
        uid, target_id, err = await _admin_target(request)
        # `err is not None`, а НЕ `if err`: aiohttp StreamResponse определяет
        # __len__, поэтому у ответа с пустым на этот момент телом bool() == False.
        # Проверка молча не срабатывала — 403 создавался и выбрасывался, а
        # выполнение шло дальше как у администратора.
        if err is not None:
            return err
        try:
            from database import db as _db
            await _db.revoke_plan_from_user(pool, target_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("admin_user_revoke uid=%d target=%d", uid, target_id)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_user_ban(request: web.Request) -> web.Response:
        """Забанить пользователя (нельзя банить админов и себя)."""
        uid, target_id, err = await _admin_target(request)
        # `err is not None`, а НЕ `if err`: aiohttp StreamResponse определяет
        # __len__, поэтому у ответа с пустым на этот момент телом bool() == False.
        # Проверка молча не срабатывала — 403 создавался и выбрасывался, а
        # выполнение шло дальше как у администратора.
        if err is not None:
            return err
        if target_id == uid or _is_admin(target_id):
            return _err("Нельзя забанить администратора", 400)
        try:
            from database import db as _db
            await _db.ban_user(pool, target_id, uid, "Забанен из mini app")
            return _json_resp({"ok": True})
        except Exception:
            log.exception("admin_user_ban uid=%d target=%d", uid, target_id)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_user_unban(request: web.Request) -> web.Response:
        """Разбанить пользователя."""
        uid, target_id, err = await _admin_target(request)
        # `err is not None`, а НЕ `if err`: aiohttp StreamResponse определяет
        # __len__, поэтому у ответа с пустым на этот момент телом bool() == False.
        # Проверка молча не срабатывала — 403 создавался и выбрасывался, а
        # выполнение шло дальше как у администратора.
        if err is not None:
            return err
        try:
            from database import db as _db
            await _db.unban_user(pool, target_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("admin_user_unban uid=%d target=%d", uid, target_id)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_audit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            rows = await pool.fetch(
                """SELECT ol.op_id, ol.step_num, ol.target, ol.status, ol.message, ol.created_at,
                          oq.owner_id, oq.op_type, oq.label
                   FROM operation_log ol
                   JOIN operation_queue oq ON oq.id = ol.op_id
                   WHERE ol.created_at > NOW() - INTERVAL '24 hours'
                   ORDER BY ol.created_at DESC LIMIT 100""")
            return _json_resp({"audit": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception:
            log.exception("admin_audit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_ops_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            rows = await pool.fetch(
                """SELECT op_type, COUNT(*) as total,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                          AVG(EXTRACT(EPOCH FROM (finished_at - started_at))) as avg_duration_s
                   FROM operation_queue
                   WHERE created_at > NOW() - INTERVAL '7 days'
                   GROUP BY op_type
                   ORDER BY total DESC""")
            return _json_resp({"stats": [
                {k: (round(float(v), 2) if k == "avg_duration_s" else int(v) if v is not None else 0) for k, v in dict(r).items()}
                for r in rows
            ]})
        except Exception:
            log.exception("admin_ops_stats uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/admin/users", admin_users)
    app.router.add_get("/api/miniapp/admin/stats", admin_stats)
    app.router.add_get("/api/miniapp/admin/user/{user_id}", admin_user_detail)
    app.router.add_post("/api/miniapp/admin/broadcast", admin_broadcast)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/grant", admin_user_grant)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/revoke", admin_user_revoke)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/ban", admin_user_ban)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/unban", admin_user_unban)
    app.router.add_get("/api/miniapp/admin/audit", admin_audit)
    app.router.add_get("/api/miniapp/admin/ops/stats", admin_ops_stats)
