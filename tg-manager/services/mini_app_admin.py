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

import json
import re

from aiohttp import web


_ACTION_RU = {
    "admin_session_granted": "вход в админку по секретной фразе",
    "grant": "выдал подписку", "revoke": "отозвал подписку",
    "ban": "заблокировал пользователя", "unban": "разблокировал пользователя",
    "broadcast": "рассылка всем пользователям",
}

# Записи слоя аудита мини-аппа (admin_audit_middleware в mini_app_api) — это
# «miniapp:POST /api/miniapp/admin/user/5/ban». Журнал печатал их сырыми.
_MINIAPP_PATH_RU = (
    (re.compile(r"/admin/user/(\d+)/grant$"), "выдал подписку"),
    (re.compile(r"/admin/user/(\d+)/revoke$"), "отозвал подписку"),
    (re.compile(r"/admin/user/(\d+)/ban$"), "заблокировал пользователя"),
    (re.compile(r"/admin/user/(\d+)/unban$"), "разблокировал пользователя"),
    (re.compile(r"/admin/broadcast$"), "рассылка всем пользователям"),
    (re.compile(r"/admin/report/(\d+)/status$"), "сменил статус отчёта об ошибке #{0}"),
)

# Статусы платежа (payments.status, CHECK в schema_v13) — словами на сервере:
# экран печатает подпись, а не «confirming».
PAY_STATUS_RU = {
    "pending": "ждёт оплаты", "confirming": "подтверждается",
    "confirmed": "оплачен", "expired": "истёк", "failed": "ошибка",
}
# Статусы отчёта об ошибке (error_reports.status) — тот же набор, что у
# админки в боте (bot/handlers/admin.py, _ERR_STATUS_LABELS).
REPORT_STATUS_RU = {
    "new": "новый", "viewing": "смотрю", "fixing": "в работе",
    "fixed": "исправлен", "duplicate": "дубликат",
}


# Окна сводки операций: только эти три. Проверяем ДО запроса и подставляем
# уже число — чтобы «окно» из адреса никогда не попадало в SQL текстом.
_OPS_STATS_DAYS = ("1", "7", "30")


def _iso(v):
    return v.isoformat() if v is not None else None




def _admin_action_ru(action: str | None) -> str:
    """Действие админа словами: «bot:cmd:/backup» → «Команда бота: /backup»."""
    a = str(action or "")
    if a.startswith("miniapp:"):
        path = a.split(" ", 1)[-1]
        for rx, ru in _MINIAPP_PATH_RU:
            m = rx.search(path)
            if m:
                ru = ru.format(*m.groups())
                return "Мини-апп: " + ru
        return "Мини-апп: " + path.replace("/api/miniapp/", "")
    if a.startswith("bot:cmd:"):
        return "Команда бота: " + a[len("bot:cmd:"):]
    if a.startswith("bot:cb:"):
        return "Кнопка админки в боте: " + a[len("bot:cb:"):]
    for key, ru in _ACTION_RU.items():
        if a == key or a.endswith(":" + key) or a.startswith(key + ":"):
            return ru[:1].upper() + ru[1:]
    return a or "—"


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать админские маршруты мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _admin_broadcast_core, _err, _get_uid, _is_admin,
        _json_resp, bad_body_response, log,
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
        # Окно считалось ровно за семь дней, и выбрать его было нечем: экран
        # сводки не имел НИ ОДНОГО действия — ни переключателя, ни обновления
        # (обход мерил: действий 0). Разрешаем только три значения, и список
        # проверяем до запроса: число из адреса в SQL не подставляем.
        days_raw = (request.query.get("days") or "7").strip()
        if days_raw not in _OPS_STATS_DAYS:
            return _err("Окно сводки бывает 1, 7 или 30 дней", 400)
        days = int(days_raw)
        try:
            rows = await pool.fetch(
                """SELECT op_type, COUNT(*) as total,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                          AVG(EXTRACT(EPOCH FROM (finished_at - started_at))) as avg_duration_s
                   FROM operation_queue
                   WHERE created_at > NOW() - make_interval(days => $1)
                   GROUP BY op_type
                   ORDER BY total DESC""", days)
            # Раньше здесь КАЖДОЕ поле строки приводилось к int, включая
            # op_type («mass_invite») — ручка падала с 500 при любом вызове.
            # Экрана у неё не было, и этого никто не видел.
            from services.operation_bus import OP_REGISTRY
            out = []
            for r in rows:
                total = int(r["total"] or 0)
                failed = int(r["failed"] or 0)
                out.append({
                    "op_type": r["op_type"],
                    "type_ru": (OP_REGISTRY.get(r["op_type"], {}) or {}).get("description") or r["op_type"],
                    "total": total, "done": int(r["done"] or 0), "failed": failed,
                    "fail_pct": round(100.0 * failed / total, 1) if total else 0.0,
                    "avg_duration_s": (round(float(r["avg_duration_s"]), 1)
                                       if r["avg_duration_s"] is not None else None),
                })
            return _json_resp({"stats": out, "days": days})
        except Exception:
            log.exception("admin_ops_stats uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def admin_actions(request: web.Request) -> web.Response:
        """Журнал действий админов (admin_audit_log): кто, что, над кем, когда.

        В мини-аппе его не было вовсе — только командой бота /admin_log. А
        именно его открывают, когда доступ мог утечь: что успели сделать.
        """
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            limit = max(1, min(int(request.query.get("limit", "100")), 500))
            offset = max(0, int(request.query.get("offset", "0")))
        except (TypeError, ValueError):
            return _err("Неверные параметры страницы", 400)
        try:
            rows = await pool.fetch(
                """SELECT l.id, l.admin_id, l.action, l.target_user_id, l.source, l.created_at,
                          l.details,
                          pa.username AS admin_username, pt.username AS target_username
                   FROM admin_audit_log l
                   LEFT JOIN platform_users pa ON pa.user_id = l.admin_id
                   LEFT JOIN platform_users pt ON pt.user_id = l.target_user_id
                   ORDER BY l.created_at DESC, l.id DESC LIMIT $1 OFFSET $2""", limit, offset)
            total = await pool.fetchval("SELECT COUNT(*) FROM admin_audit_log")
            def _refused(details) -> bool:
                # Слой аудита пишет и отказы (с кодом ответа): «заблокировал»
                # над отказом ручки — неправда, поэтому отказ называется.
                try:
                    d = json.loads(details) if isinstance(details, str) else (details or {})
                    return int(d.get("status") or 0) >= 400
                except (TypeError, ValueError, AttributeError):
                    return False

            return _json_resp({"actions": [
                {**{k: v for k, v in dict(r).items() if k != "details"},
                 "action_ru": _admin_action_ru(r["action"]) + (
                     " — не выполнено" if _refused(dict(r).get("details")) else ""),
                 "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows], "total": int(total or 0),
                "has_more": offset + len(rows) < int(total or 0)})
        except Exception:
            log.exception("admin_actions uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    from services import tiers as _tiers

    async def admin_payments(request: web.Request) -> web.Response:
        """Платежи платформы: кто, что, сколько и в каком состоянии.

        В мини-аппе админа их не было вовсе: «застрявший» платёж (деньги ушли,
        подписка не включилась) искали по логам. Срез по статусу — тот же
        фильтр, что у итога, и сводка выручки по подтверждённым.
        """
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 200))
            offset = max(0, int(request.query.get("offset", "0")))
        except (TypeError, ValueError):
            return _err("Неверные параметры страницы", 400)
        status = (request.query.get("status") or "").strip()
        if status and status not in PAY_STATUS_RU:
            return _err("Неизвестный статус платежа", 400)
        try:
            rows = await pool.fetch(
                """SELECT p.id, p.user_id, pu.username, pu.first_name, p.plan, p.tier,
                          p.period_months, p.currency, p.amount_crypto, p.amount_usd,
                          p.status, p.tx_hash, p.created_at, p.confirmed_at
                   FROM payments p
                   LEFT JOIN platform_users pu ON pu.user_id = p.user_id
                   WHERE ($3 = '' OR p.status = $3)
                   ORDER BY p.created_at DESC, p.id DESC LIMIT $1 OFFSET $2""",
                limit, offset, status)
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM payments p WHERE ($1 = '' OR p.status = $1)", status)
            by_status = await pool.fetch(
                "SELECT status, COUNT(*) AS n FROM payments GROUP BY status")
            rev = await pool.fetchrow(
                """SELECT COALESCE(SUM(amount_usd) FILTER (
                              WHERE confirmed_at >= now() - interval '30 days'), 0) AS usd_30d,
                          COALESCE(SUM(amount_usd), 0) AS usd_all,
                          COUNT(DISTINCT user_id) AS payers
                   FROM payments WHERE status = 'confirmed'""")
        except Exception:
            log.exception("admin_payments uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        total = int(total or 0)
        return _json_resp({
            "payments": [{
                "id": r["id"], "user_id": r["user_id"], "username": r["username"] or "",
                "first_name": r["first_name"] or "", "plan": r["plan"] or "",
                "tier": r["tier"] or "",
                # Название тарифа словами («Про»), а не ключом из базы («pro»).
                "tier_name": _tiers.name(_tiers.coerce_tier(r["tier"] or r["plan"]))
                if _tiers.coerce_tier(r["tier"] or r["plan"]) else (r["plan"] or ""),
                "period_months": int(r["period_months"] or 0),
                "currency": r["currency"] or "",
                "amount_crypto": float(r["amount_crypto"] or 0),
                "amount_usd": float(r["amount_usd"] or 0),
                "status": r["status"], "status_ru": PAY_STATUS_RU.get(r["status"], r["status"]),
                "has_tx": bool(r["tx_hash"]),
                "created_at": _iso(r["created_at"]), "confirmed_at": _iso(r["confirmed_at"]),
            } for r in rows],
            "total": total, "has_more": offset + len(rows) < total,
            "by_status": {r["status"]: int(r["n"]) for r in by_status},
            "revenue": {"usd_30d": float(rev["usd_30d"] or 0) if rev else 0.0,
                        "usd_all": float(rev["usd_all"] or 0) if rev else 0.0,
                        "payers": int(rev["payers"] or 0) if rev else 0},
        })

    async def admin_reports(request: web.Request) -> web.Response:
        """Отчёты об ошибках от пользователей («🐛 Сообщить об ошибке»).

        Пользователь отправлял отчёт из мини-аппа, а прочитать его можно было
        только в боте (/admin → отчёты). Здесь тот же список со срезом по
        статусу и сменой статуса.
        """
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 200))
            offset = max(0, int(request.query.get("offset", "0")))
        except (TypeError, ValueError):
            return _err("Неверные параметры страницы", 400)
        status = (request.query.get("status") or "").strip()
        if status and status not in REPORT_STATUS_RU:
            return _err("Неизвестный статус отчёта", 400)
        try:
            rows = await pool.fetch(
                """SELECT er.id, er.user_id, pu.username, pu.first_name, er.description,
                          er.status, er.notes, er.context,
                          (er.screenshot_id IS NOT NULL) AS has_screenshot,
                          er.created_at, er.updated_at
                   FROM error_reports er
                   LEFT JOIN platform_users pu ON pu.user_id = er.user_id
                   WHERE ($3 = '' OR er.status = $3)
                   ORDER BY er.created_at DESC, er.id DESC LIMIT $1 OFFSET $2""",
                limit, offset, status)
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM error_reports er WHERE ($1 = '' OR er.status = $1)", status)
            by_status = await pool.fetch(
                "SELECT status, COUNT(*) AS n FROM error_reports GROUP BY status")
        except Exception:
            log.exception("admin_reports uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        total = int(total or 0)
        return _json_resp({
            "reports": [{
                "id": r["id"], "user_id": r["user_id"], "username": r["username"] or "",
                "first_name": r["first_name"] or "",
                "description": (r["description"] or "")[:2000],
                "status": r["status"], "status_ru": REPORT_STATUS_RU.get(r["status"], r["status"]),
                "notes": (r["notes"] or "")[:1000], "has_screenshot": bool(r["has_screenshot"]),
                "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            } for r in rows],
            "total": total, "has_more": offset + len(rows) < total,
            "by_status": {r["status"]: int(r["n"]) for r in by_status},
            "statuses": REPORT_STATUS_RU,
        })

    async def admin_report_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            report_id = int(request.match_info["report_id"])
        except (KeyError, TypeError, ValueError):
            return _err("Неверный номер отчёта", 400)
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return bad if bad is not None else _err("Не удалось разобрать запрос", 400)
        new_status = str((data or {}).get("status") or "").strip()
        if new_status not in REPORT_STATUS_RU:
            return _err("Неизвестный статус отчёта", 400)
        notes = (data or {}).get("notes")
        notes = str(notes).strip()[:1000] if notes not in (None, "") else None
        try:
            from database import db as _db
            if not await _db.update_error_report_status(pool, report_id, new_status, notes):
                return _err("Отчёт не найден", 404)
            # Журнал админов пишет общий слой (admin_audit_middleware) — для
            # каждого изменяющего запроса к /admin/*; вторая запись была бы дублем.
        except Exception:
            log.exception("admin_report_status uid=%d report=%d", uid, report_id)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "status": new_status,
                           "status_ru": REPORT_STATUS_RU[new_status]})

    app.router.add_get("/api/miniapp/admin/payments", admin_payments)
    app.router.add_get("/api/miniapp/admin/reports", admin_reports)
    app.router.add_post("/api/miniapp/admin/report/{report_id}/status", admin_report_status)
    app.router.add_get("/api/miniapp/admin/actions", admin_actions)
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
