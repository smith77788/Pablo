"""«Запланировано» — один экран для всего, что продукт сделает сам по времени.

ЗАЧЕМ. Отложенные и повторяемые действия у продукта уже были, но жили в трёх
разных местах и были видны только оттуда, где их заводили:

* отложенные рассылки бота — `scheduled_broadcasts`, список внутри карточки
  КАЖДОГО бота отдельно (`/api/miniapp/bot/{id}/schedules`);
* отложенная публикация и рассылка — `operation_queue.scheduled_for`, видно
  только в общей очереди операций, вперемешку с историей;
* повторяемые круги (автопостинг, рассылка по кругу, авто-мониторинг) —
  `params.repeat_interval_min`, следующий круг ставит `op_worker`.

Ответить на вопрос «что у меня запланировано на завтра» было нечем, перенести
время — тоже: только отменить и завести заново. Здесь — одна сводка, перенос
времени, смена текста и повтора, отмена и итог последних запусков.

ВТОРОГО ПЛАНИРОВЩИКА НЕ ЗАВОДИМ. Рассылки бота по-прежнему запускает
`services/scheduler.py`, операции — `op_worker` (круги — через
`_reschedule_recurring`). Этот модуль только читает их строки и правит поля
ещё НЕ занятых строк условным UPDATE'ом (`status='pending'`): строку, которую
исполнитель уже взял, правка не трогает и честно отвечает «уже выполняется».

«Повторить по расписанию» ставит новую операцию ОБЩЕЙ ДВЕРЬЮ шины
(`operation_bus.submit`): тариф, предохранитель, окно двойного тапа и реестр
типов работают как у любого запуска. Повтор разрешён только типам, которые
исполнитель умеет зацикливать (`op_worker._RECURRING_OK_OPS`).
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from aiohttp import web

# Повтор: не чаще раза в час, не реже раза в месяц. Ноль — разовый запуск.
MIN_REPEAT_MIN = 60
MAX_REPEAT_MIN = 60 * 24 * 30
# Как далеко вперёд можно планировать.
MAX_AHEAD_DAYS = 90
# Длина текста рассылки бота (лимит сообщения Telegram).
MAX_TEXT = 4096
# Сколько завершённых запусков показывать в «Последних запусках».
HISTORY_DAYS = 14
HISTORY_LIMIT = 30

_ACTIVE_OP = ("pending", "scheduled", "paused", "running")


class PlanError(ValueError):
    """Ввод не принят. Текст — по-русски, его видит владелец."""


def parse_when(raw: Any, *, now: _dt.datetime | None = None) -> _dt.datetime:
    """ISO-время запуска с экрана → aware datetime, округлённое вверх до минуты.

    Округление то же, что у `operation_bus.quantized_schedule_iso`: окно
    идемпотентности шины сравнивает время точно, и два тапа подряд должны
    давать одно время, а не два запуска.
    """
    s = str(raw or "").strip()
    if not s:
        raise PlanError("Укажите время запуска")
    try:
        when = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise PlanError("Не удалось разобрать время запуска") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    if when.second or when.microsecond:
        when = when.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if when <= now:
        raise PlanError("Время запуска должно быть в будущем")
    if when > now + _dt.timedelta(days=MAX_AHEAD_DAYS):
        raise PlanError(f"Планировать можно не дальше чем на {MAX_AHEAD_DAYS} дней вперёд")
    return when


def parse_repeat(raw: Any) -> int:
    """Период повтора в минутах: 0 — разово, иначе от часа до месяца."""
    if raw in (None, "", "none"):
        return 0
    try:
        m = int(raw)
    except (TypeError, ValueError):
        raise PlanError("Период повтора — целое число минут") from None
    if m <= 0:
        return 0
    if m < MIN_REPEAT_MIN:
        raise PlanError("Повторять можно не чаще раза в час")
    if m > MAX_REPEAT_MIN:
        raise PlanError("Повторять можно не реже раза в 30 дней")
    return m


def _params(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, (_dt.datetime, _dt.date)) else None


def _bot_name(row) -> str:
    u = (row.get("username") or "").strip()
    if u:
        return "@" + u
    return (row.get("first_name") or "").strip() or f"бот {row.get('bot_id')}"


def broadcast_item(row) -> dict:
    """Строка `scheduled_broadcasts` (+ бот и итог рассылки) → пункт экрана."""
    status = str(row["status"])
    res = None
    if row.get("bc_status") is not None:
        res = {
            "status": row["bc_status"],
            "sent": int(row.get("bc_sent") or 0),
            "failed": int(row.get("bc_failed") or 0),
            "total": int(row.get("bc_total") or 0),
        }
    return {
        "kind": "broadcast",
        "id": int(row["id"]),
        "title": "Рассылка в боте " + _bot_name(row),
        "bot_id": int(row["bot_id"]),
        "text": row.get("message_text") or "",
        "when": _iso(row["execute_at"]),
        "repeat_min": int(row.get("repeat_interval_min") or 0),
        "status": status,
        "editable": status == "pending",
        "cancellable": status == "pending",
        "result": res,
    }


def operation_item(row, *, now: _dt.datetime | None = None) -> dict:
    """Строка `operation_queue` → пункт экрана."""
    prm = _params(row.get("params"))
    try:
        rmin = int(prm.get("repeat_interval_min") or 0)
    except (TypeError, ValueError):
        rmin = 0
    status = str(row["status"])
    when = row.get("scheduled_for")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    waiting = status in ("pending", "scheduled") and isinstance(when, _dt.datetime) and when > now
    text = prm.get("text") or prm.get("message_text") or prm.get("message") or ""
    return {
        "kind": "operation",
        "id": int(row["id"]),
        "op_type": row["op_type"],
        "title": (row.get("label") or "").strip(),
        "text": text if isinstance(text, str) else "",
        "when": _iso(when) if waiting or status == "paused" else _iso(
            row.get("finished_at") or row.get("started_at") or when),
        "repeat_min": rmin,
        "status": status,
        # Править можно только то, что ещё не взято в работу и ждёт своего времени.
        "editable": waiting,
        "cancellable": status in ("pending", "scheduled", "paused"),
        "result": {
            "done": int(row.get("done_items") or 0),
            "total": int(row.get("total_items") or 0),
            "error": (row.get("error_msg") or "")[:200] or None,
        },
    }


def _first_when(item: dict) -> str:
    return item.get("when") or "9999"


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _err, _get_uid, _json_resp, log
    from services import op_status as _ops
    from services import operation_bus as _obus

    def _recurring_ok() -> frozenset:
        from services.op_worker import _RECURRING_OK_OPS
        return _RECURRING_OK_OPS

    async def _plan_body(request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise PlanError("Не удалось разобрать запрос") from None
        if not isinstance(data, dict):
            raise PlanError("Не удалось разобрать запрос")
        return data

    def _id(request, key: str) -> int:
        try:
            return int(request.match_info[key])
        except (KeyError, ValueError):
            raise PlanError("Неверный идентификатор") from None

    # ── Сводка ───────────────────────────────────────────────────────────────

    async def planner_list(request: web.Request) -> web.Response:
        """GET /api/miniapp/planner — что запланировано и чем кончились запуски."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        now = _dt.datetime.now(_dt.timezone.utc)
        upcoming: list[dict] = []
        history: list[dict] = []
        try:
            b_rows = await pool.fetch(
                """SELECT sb.id, sb.bot_id, sb.message_text, sb.execute_at, sb.status,
                          COALESCE(sb.repeat_interval_min, 0) AS repeat_interval_min,
                          mb.username, mb.first_name,
                          b.status AS bc_status, b.sent_count AS bc_sent,
                          b.failed_count AS bc_failed, b.total_users AS bc_total
                     FROM scheduled_broadcasts sb
                     JOIN managed_bots mb ON mb.bot_id = sb.bot_id
                          AND mb.added_by = $1 AND mb.deleted_at IS NULL
                     LEFT JOIN broadcasts b ON b.id = sb.broadcast_id
                    WHERE sb.created_by = $1
                      AND (sb.status IN ('pending', 'processing')
                           OR sb.execute_at >= now() - make_interval(days => $2))
                    ORDER BY sb.execute_at DESC
                    LIMIT 200""", uid, HISTORY_DAYS)
        except Exception:
            log.exception("planner_list: рассылки uid=%s", uid)
            return _err("Не удалось загрузить запланированное", 500)
        for r in b_rows:
            it = broadcast_item(r)
            (upcoming if it["status"] in ("pending", "processing") else history).append(it)

        try:
            o_rows = await pool.fetch(
                f"""SELECT id, op_type, status, label, params, total_items, done_items,
                          scheduled_for, started_at, finished_at,
                          LEFT({_ops.sql_error_reason()}, 200) AS error_msg
                     FROM operation_queue
                    WHERE owner_id = $1
                      AND (
                        (status IN ('pending', 'scheduled', 'paused', 'running')
                         AND (scheduled_for > now()
                              OR COALESCE((params->>'repeat_interval_min')::int, 0) > 0))
                        OR
                        (status IN ('done', 'failed', 'cancelled', 'partial')
                         AND COALESCE((params->>'repeat_interval_min')::int, 0) > 0
                         AND COALESCE(finished_at, created_at)
                             >= now() - make_interval(days => $2))
                      )
                    ORDER BY id DESC
                    LIMIT 200""", uid, HISTORY_DAYS)
        except Exception:
            log.exception("planner_list: операции uid=%s", uid)
            return _err("Не удалось загрузить запланированное", 500)
        for r in o_rows:
            it = operation_item(r, now=now)
            (upcoming if it["status"] in _ACTIVE_OP else history).append(it)

        upcoming.sort(key=_first_when)
        history.sort(key=lambda i: i.get("when") or "", reverse=True)
        history = history[:HISTORY_LIMIT]
        day_end = now + _dt.timedelta(hours=24)
        next_24h = sum(1 for i in upcoming
                       if i.get("when") and i["status"] in ("pending", "scheduled")
                       and i["when"] <= day_end.isoformat())
        return _json_resp({
            "upcoming": upcoming,
            "history": history,
            "counts": {
                "upcoming": len(upcoming),
                "recurring": sum(1 for i in upcoming if i["repeat_min"] > 0),
                "next_24h": next_24h,
                "failed": sum(1 for i in history if i["status"] in ("failed", "missed")),
            },
        })

    # ── Рассылка бота: правка и отмена ───────────────────────────────────────

    async def planner_broadcast_edit(request: web.Request) -> web.Response:
        """POST /api/miniapp/planner/broadcast/{id} — новое время, текст, повтор."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sid = _id(request, "sch_id")
            data = await _plan_body(request)
            when = parse_when(data.get("run_at"))
            repeat = parse_repeat(data.get("repeat_min"))
            text = data.get("text")
            if text is not None:
                text = str(text).strip()
                if not text:
                    raise PlanError("Текст рассылки не может быть пустым")
                if len(text) > MAX_TEXT:
                    raise PlanError(f"Текст длиннее {MAX_TEXT} знаков")
        except PlanError as e:
            return _err(str(e), 400)
        try:
            # Условие status='pending' — та же граница, по которой планировщик
            # занимает строку (pending → processing): занятую правка не тронет.
            row = await pool.fetchrow(
                """UPDATE scheduled_broadcasts
                      SET execute_at = $3, repeat_interval_min = $4,
                          message_text = COALESCE($5, message_text)
                    WHERE id = $1 AND created_by = $2 AND status = 'pending'
                RETURNING id""", sid, uid, when, repeat, text)
            if not row:
                cur = await pool.fetchval(
                    "SELECT status FROM scheduled_broadcasts WHERE id=$1 AND created_by=$2",
                    sid, uid)
                if cur is None:
                    return _err("Рассылка не найдена", 404)
                return _err("Рассылка уже отправляется или завершена — менять поздно", 409)
            return _json_resp({"ok": True, "id": sid, "run_at": when.isoformat(),
                               "repeat_min": repeat})
        except Exception:
            log.exception("planner_broadcast_edit sch=%s uid=%s", sid, uid)
            return _err("Не удалось сохранить изменения", 500)

    # ── Операция: перенос времени и повтора ──────────────────────────────────

    async def planner_operation_edit(request: web.Request) -> web.Response:
        """POST /api/miniapp/planner/operation/{id} — перенести запуск, сменить повтор."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = _id(request, "op_id")
            data = await _plan_body(request)
            when = parse_when(data.get("run_at"))
            repeat = parse_repeat(data.get("repeat_min"))
        except PlanError as e:
            return _err(str(e), 400)
        try:
            op_type = await pool.fetchval(
                "SELECT op_type FROM operation_queue WHERE id=$1 AND owner_id=$2", op_id, uid)
            if op_type is None:
                return _err("Операция не найдена", 404)
            if repeat and op_type not in _recurring_ok():
                return _err("Эту операцию нельзя повторять по расписанию", 400)
            # Только ждущая своего времени: занятую исполнителем (running) или
            # уже подошедшую (scheduled_for в прошлом — поллер вот-вот возьмёт)
            # не трогаем, чтобы не сдвинуть работу из-под исполнителя.
            row = await pool.fetchrow(
                """UPDATE operation_queue
                      SET scheduled_for = $3,
                          params = jsonb_set(COALESCE(params, '{}'::jsonb),
                                             '{repeat_interval_min}', to_jsonb($4::int))
                    WHERE id = $1 AND owner_id = $2
                      AND status IN ('pending', 'scheduled')
                      AND scheduled_for > now()
                RETURNING id""", op_id, uid, when, repeat)
            if not row:
                return _err("Операция уже выполняется или завершена — переносить поздно", 409)
            return _json_resp({"ok": True, "id": op_id, "run_at": when.isoformat(),
                               "repeat_min": repeat})
        except Exception:
            log.exception("planner_operation_edit op=%s uid=%s", op_id, uid)
            return _err("Не удалось сохранить изменения", 500)

    async def planner_operation_stop_repeat(request: web.Request) -> web.Response:
        """POST /api/miniapp/planner/operation/{id}/stop_repeat — снять повтор.

        Текущий круг доработает (или запустится в своё время), следующий не
        будет поставлен: `_reschedule_recurring` выходит при нулевом интервале.
        Нужно для идущего круга, который нельзя ни перенести, ни отменить
        без потери начатой работы.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = _id(request, "op_id")
        except PlanError as e:
            return _err(str(e), 400)
        try:
            row = await pool.fetchrow(
                """UPDATE operation_queue
                      SET params = jsonb_set(COALESCE(params, '{}'::jsonb),
                                             '{repeat_interval_min}', '0'::jsonb)
                    WHERE id = $1 AND owner_id = $2
                      AND status IN ('pending', 'scheduled', 'paused', 'running')
                      AND COALESCE((params->>'repeat_interval_min')::int, 0) > 0
                RETURNING id""", op_id, uid)
            if not row:
                return _err("У операции нет активного повтора", 409)
            return _json_resp({"ok": True, "id": op_id})
        except Exception:
            log.exception("planner_operation_stop_repeat op=%s uid=%s", op_id, uid)
            return _err("Не удалось снять повтор", 500)

    # ── Повторить завершённую операцию по расписанию ─────────────────────────

    async def planner_candidates(request: web.Request) -> web.Response:
        """GET /api/miniapp/planner/candidates — что можно поставить на расписание.

        Последние завершённые операции тех типов, которые исполнитель умеет
        повторять: публикации, рассылки, объявления, проверки флота.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT DISTINCT ON (op_type, COALESCE(label, ''))
                          id, op_type, label, params, total_items, done_items,
                          status, finished_at
                     FROM operation_queue
                    WHERE owner_id = $1 AND op_type = ANY($2::text[])
                      AND status IN ('done', 'partial')
                    ORDER BY op_type, COALESCE(label, ''), id DESC
                    LIMIT 100""", uid, sorted(_recurring_ok()))
        except Exception:
            log.exception("planner_candidates uid=%s", uid)
            return _err("Не удалось загрузить операции", 500)
        items = []
        for r in rows:
            prm = _params(r["params"])
            text = prm.get("text") or prm.get("message_text") or ""
            items.append({
                "id": int(r["id"]), "op_type": r["op_type"],
                "title": (r["label"] or "").replace(" ↻", "").strip(),
                "text": (text if isinstance(text, str) else "")[:160],
                "total": int(r["total_items"] or 0),
                "finished_at": _iso(r["finished_at"]),
            })
        items.sort(key=lambda i: i["finished_at"] or "", reverse=True)
        return _json_resp({"items": items[:30]})

    async def planner_schedule_operation(request: web.Request) -> web.Response:
        """POST /api/miniapp/planner/operation/{id}/schedule — та же работа по расписанию.

        Тело: {run_at: ISO, repeat_min: 0|60..43200}. Новая операция ставится
        шиной с `scheduled_for`; при повторе следующий круг поставит op_worker.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = _id(request, "op_id")
            data = await _plan_body(request)
            when = parse_when(data.get("run_at"))
            repeat = parse_repeat(data.get("repeat_min"))
        except PlanError as e:
            return _err(str(e), 400)
        try:
            src = await pool.fetchrow(
                "SELECT op_type, status, label, params, total_items FROM operation_queue "
                "WHERE id=$1 AND owner_id=$2", op_id, uid)
            if not src:
                return _err("Операция не найдена", 404)
            if src["op_type"] not in _recurring_ok():
                return _err("Эту операцию нельзя ставить на расписание", 400)
            if src["status"] not in ("done", "partial"):
                return _err("Поставить на расписание можно завершённую операцию", 409)
            # Одна живая копия на источник: второе нажатие не заводит вторую
            # цепочку той же работы (каналы получали бы посты вдвое чаще).
            live = await pool.fetchval(
                """SELECT id FROM operation_queue
                    WHERE owner_id=$1
                      AND status IN ('pending', 'scheduled', 'paused', 'running')
                      AND params->>'cloned_from_op' = $2::text
                    LIMIT 1""", uid, str(op_id))
            if live:
                return _err(f"Эта операция уже запланирована (#{int(live)}) — "
                            "перенесите или отмените её на экране «Запланировано»", 409)
            from services import op_params_view as _opv
            params = _opv.clone_params(src["op_type"], _params(src["params"]), None)
            params["cloned_from_op"] = op_id
            if repeat:
                params["repeat_interval_min"] = repeat
            base = (src["label"] or "").replace(" ↻", "").strip()
            label = (base[:80] + " · " if base else "") + "по расписанию"
            try:
                new_id = await _obus.submit(
                    pool, uid, src["op_type"], params,
                    total_items=int(src["total_items"] or 0),
                    scheduled_for=when, label=label)
            except _obus.ProjectPublishReviewRequired as exc:
                return _err(str(exc), 409)
            except PermissionError as exc:
                return _err(str(exc) or "Требуется подписка", 403)
            except ValueError:
                return _err("Этот тип операции больше не поддерживается", 400)
            return _json_resp({"ok": True, "new_id": int(new_id),
                               "run_at": when.isoformat(), "repeat_min": repeat})
        except Exception:
            log.exception("planner_schedule_operation op=%s uid=%s", op_id, uid)
            return _err("Не удалось поставить на расписание", 500)

    app.router.add_get("/api/miniapp/planner", planner_list)
    app.router.add_get("/api/miniapp/planner/candidates", planner_candidates)
    app.router.add_post("/api/miniapp/planner/broadcast/{sch_id}", planner_broadcast_edit)
    app.router.add_post("/api/miniapp/planner/operation/{op_id}", planner_operation_edit)
    app.router.add_post("/api/miniapp/planner/operation/{op_id}/stop_repeat",
                        planner_operation_stop_repeat)
    app.router.add_post("/api/miniapp/planner/operation/{op_id}/schedule",
                        planner_schedule_operation)
