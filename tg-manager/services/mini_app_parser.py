"""Парсинг аудитории, сетка ботов, топология и пульс — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 14 маршрутов четырёх блоков, которые
владелец видит как одну работу «собрать людей и посмотреть, как связана
сеть».

* `/api/miniapp/parser/*` — заявка на парсинг, прогоны, собранная аудитория
  с фильтрами и её выгрузка. Фильтры считает
  `services.audience_filters.parsed_audience_filters` — одна дверь для
  показа и для выгрузки, иначе выгрузка отдала бы не то, что на экране
  (`tests/test_parsed_audience_filters.py`).
* `/api/miniapp/mesh/*` — задачи сетки ботов: постановка, список, карточка,
  снятие.
* `/api/miniapp/topology*` — связи и узлы сети.
* `/api/miniapp/organism/*` — пульс, сводка и скрытие подсказки.

Вместе с обработчиками уехало замыкание `_mesh_bot_names(uid)` — имена
ботов владельца одним запросом для списка и для карточки задачи. Его не
зовёт больше никто: `_own_mesh` (скоуп правила сетки) уехал раньше в
`services/mini_app_content.py` вместе со своими ручками.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import json

from aiohttp import web

from services import operation_bus as _obus
from services.audience_filters import parsed_audience_filters


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты парсинга, сетки, топологии и пульса."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _csv_resp, _err, _get_uid, _json_resp, _list_limit,
        _list_offset, _safe_count, _safe_fetch, _safe_fetchrow, log,
    )
    from services.security import validate_integer


    async def topology_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            accs = await pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", uid)
            channels = await pool.fetchval("SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
            bots = await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
            # Channel-to-account links: managed_channels rows with an assigned account
            try:
                links = await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id IS NOT NULL",
                    uid,
                )
            except Exception as e:
                log.warning("topology_overview links uid=%d: %s", uid, e)
                links = 0
            # Bot-user relationships
            try:
                bot_users_total = await pool.fetchval(
                    """SELECT COUNT(*) FROM bot_users bu
                       JOIN managed_bots b ON b.bot_id=bu.bot_id
                       WHERE b.added_by=$1""",
                    uid,
                )
            except Exception as e:
                log.warning("topology_overview bot_users uid=%d: %s", uid, e)
                bot_users_total = 0
            return _json_resp({
                "accounts": accs, "channels": channels, "bots": bots,
                "channel_links": links, "bot_users_total": bot_users_total,
            })
        except Exception:
            log.exception("topology_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def topology_links(request: web.Request) -> web.Response:
        """Drill-down for the Topology Map: which account owns which channels/groups,
        and per-bot subscriber counts — topology_overview only returns flat totals."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT a.id AS acc_id, a.phone, a.first_name,
                          mc.id AS channel_pk, mc.channel_id, mc.title, mc.username
                   FROM tg_accounts a
                   LEFT JOIN managed_channels mc
                       ON mc.acc_id = a.id AND mc.owner_id = a.owner_id
                   WHERE a.owner_id=$1
                   ORDER BY a.id, mc.title""",
                uid,
            )
            by_acc: dict[int, dict] = {}
            for r in rows:
                acc = by_acc.setdefault(r["acc_id"], {
                    "acc_id": r["acc_id"],
                    "label": f"@{r['phone']}" if not r["first_name"] else r["first_name"],
                    "channels": [],
                })
                if r["channel_pk"] is not None:
                    acc["channels"].append({
                        "id": r["channel_pk"],
                        "channel_id": r["channel_id"],
                        "title": r["title"] or "",
                        "username": r["username"] or "",
                    })
            bot_rows = await pool.fetch(
                """SELECT b.bot_id, b.username, b.first_name,
                          COUNT(bu.user_id) FILTER (WHERE bu.is_active) AS subs
                   FROM managed_bots b
                   LEFT JOIN bot_users bu ON bu.bot_id = b.bot_id
                   WHERE b.added_by=$1
                   GROUP BY b.bot_id, b.username, b.first_name
                   ORDER BY subs DESC NULLS LAST""",
                uid,
            )
            return _json_resp({
                "accounts": list(by_acc.values()),
                "bots": [
                    {
                        "bot_id": r["bot_id"],
                        "label": f"@{r['username']}" if r["username"] else (r["first_name"] or ""),
                        "subscribers": r["subs"] or 0,
                    }
                    for r in bot_rows
                ],
            })
        except Exception:
            log.exception("topology_links uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def organism_pulse(request: web.Request) -> web.Response:
        """Живой пульс организма: единый контекст + цепочки следующих действий."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.organism import brain
            return _json_resp(await brain.pulse(pool, uid))
        except Exception:
            log.exception("organism_pulse uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def organism_digest(request: web.Request) -> web.Response:
        """Пульс-Дайджест: единый отчёт организма (флот/аудитория/рост/сеть/риски)
        с трендами неделя-к-неделе и приоритетными действиями. Read-only."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.organism import digest
            return _json_resp(await digest.build(pool, uid))
        except Exception:
            log.exception("organism_digest uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def organism_dismiss(request: web.Request) -> web.Response:
        """Отклонить подсказку — организм её больше не показывает."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        sid = str(body.get("id") or "").strip()[:64]
        if not sid:
            return _err("Нет id подсказки", 400)
        from services.organism import spine
        cur = await spine.state_get(pool, uid, "dismissed", []) or []
        if sid not in cur:
            cur.append(sid)
            await spine.state_set(pool, uid, "dismissed", cur[-100:])
        return _json_resp({"ok": True})

    async def organism_pending_open(request: web.Request) -> web.Response:
        """Куда вёл последний нудж — спасательный путь для «Открыть».

        Кнопка «Открыть» в нудже кладёт раздел в #<kind> web_app-ссылки, но
        часть клиентов Telegram заменяет хэш своими tgWebApp-параметрами, и
        раздел теряется — «Открыть» молча приводит на главную. Тогда мини-апп
        на старте, не найдя раздела в хэше, спрашивает цель здесь. Отдаём её
        РАЗОВО (сразу чистим ключ, даже если протух) — иначе следующее обычное
        открытие приложения прыгнуло бы в тот же старый нудж — и только в узком
        окне (30 мин): позже это уже не «я нажал Открыть», а просто запуск.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            import time as _time
            from services.organism import spine
            got = await spine.state_get(pool, uid, "nudge_open", None)
            if not isinstance(got, dict) or not got.get("kind"):
                return _json_resp({})
            # Разовый расход: чистим до проверки свежести.
            await spine.state_set(pool, uid, "nudge_open", None)
            try:
                at = float(got.get("at") or 0)
            except (TypeError, ValueError):
                at = 0.0
            if at <= 0 or (_time.time() - at) > 1800:
                return _json_resp({})
            out = {"kind": str(got.get("kind"))}
            if got.get("param") not in (None, ""):
                out["param"] = str(got.get("param"))
            return _json_resp(out)
        except Exception:
            log.exception("organism_pending_open uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def mesh_task_create(request: web.Request) -> web.Response:
        """Запустить цепочку между ботами.

        Приёмная половина Bot Mesh была написана целиком: auto_responder
        разбирает конверт, гасит петли, двигает шаг и пересылает дальше. А
        начать цепочку было нечем — `bot_mesh.make_envelope`/`create_task` не
        вызывал никто, ни из бота, ни из мини-аппа. Поэтому таблица всегда
        пустая, и на экране владелец видел «Задач между ботами нет».
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        origin = validate_integer(body.get("origin_bot"), min_val=1, max_val=2**63 - 1)
        if origin is None:
            return _err("Не выбран бот-инициатор")
        raw_route = body.get("route") or []
        if not isinstance(raw_route, list) or not raw_route:
            return _err("Маршрут пуст: выберите хотя бы одного получателя")
        if len(raw_route) > 8:
            return _err("В маршруте не больше восьми шагов")
        route_ids = []
        for step in raw_route:
            bid = validate_integer(step.get("bot") if isinstance(step, dict) else step,
                                   min_val=1, max_val=2**63 - 1)
            if bid is None:
                return _err("В маршруте есть неизвестный бот")
            route_ids.append(bid)
        text = (body.get("text") or "").strip()[:2000]
        # Все боты маршрута должны принадлежать владельцу: иначе цепочку можно
        # было бы направить в чужого бота.
        rows = await _safe_fetch(pool,
            "SELECT bot_id, token, username, first_name FROM managed_bots "
            "WHERE added_by=$1 AND is_active=TRUE AND bot_id = ANY($2::bigint[])",
            uid, list({origin, *route_ids}))
        known = {r["bot_id"]: r for r in (rows or [])}
        missing = [b for b in [origin, *route_ids] if b not in known]
        if missing:
            return _err("Эти боты не ваши или отключены: " + ", ".join(str(b) for b in missing))
        if route_ids[0] == origin:
            return _err("Первый получатель не может быть инициатором")

        from services import bot_mesh
        env = bot_mesh.make_envelope(
            uid, origin,
            [{"bot": b, "capability": None} for b in route_ids],
            payload={"text": text} if text else {})
        try:
            await bot_mesh.create_task(pool, env)
        except Exception:
            log.exception("mesh_task_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

        # Физическая отправка bot→bot требует включённого режима у ОБОИХ ботов
        # (@BotFather). Если Telegram отказал — цепочка не «висит запущенной»,
        # а честно помечается брошенной, и владелец видит причину.
        try:
            from services.token_vault import decrypt_token as _dt
            token = _dt(known[origin]["token"])
        except Exception:
            token = known[origin]["token"]
        sent, why = False, ""
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as sess:
                async with sess.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": route_ids[0],
                          "text": bot_mesh.encode_message(env)},
                    timeout=_aio.ClientTimeout(total=10),
                ) as resp:
                    jd = await resp.json()
            sent = bool(jd.get("ok"))
            why = str(jd.get("description") or "")[:200]
        except Exception as exc:
            why = str(exc)[:200]
        if not sent:
            try:
                await bot_mesh.drop_task(pool, env["task_id"], "send_failed")
            except Exception:
                log.debug("mesh_task_create: drop persist failed")
            return _json_resp({
                "ok": False, "task_id": env["task_id"], "sent": False,
                "error": "Telegram не принял передачу боту: " + (why or "неизвестная причина")
                         + ". Режим общения между ботами включается у обоих в @BotFather.",
            })
        try:
            await bot_mesh.record_hop(pool, env["task_id"], 0, from_bot=origin,
                                      to_bot=route_ids[0], capability=None,
                                      outcome="sent")
        except Exception:
            log.debug("mesh_task_create: hop persist failed")
        return _json_resp({"ok": True, "task_id": env["task_id"], "sent": True})

    async def mesh_task_drop(request: web.Request) -> web.Response:
        """Прервать цепочку, которая зависла на шаге."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        task_id = (request.match_info.get("task_id") or "")[:64]
        row = await _safe_fetchrow(pool,
            "SELECT status FROM bot_mesh_tasks WHERE task_id=$1 AND owner_id=$2",
            task_id, uid)
        if not row:
            return _err("Задача не найдена", 404)
        if row["status"] != "running":
            return _err("Цепочка уже не выполняется")
        from services import bot_mesh
        await bot_mesh.drop_task(pool, task_id, "stopped_by_owner")
        return _json_resp({"ok": True})

    async def mesh_tasks(request: web.Request) -> web.Response:
        """Bot Mesh: задачи, которые боты передают друг другу по маршруту.

        Владелец видит цепочку Sales→Qualification→CRM как один рабочий
        процесс; на деле это задача, идущая по ботам. Экрана у неё не было
        вовсе: задачи копились в таблице, а посмотреть, куда дошла и почему
        брошена, было негде.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        lim = _list_limit(request, 30, 500)
        rows = await _safe_fetch(pool,
            """SELECT task_id, origin_bot, route, step, depth, trace, status,
                      drop_reason, deadline_at, created_at, updated_at
                 FROM bot_mesh_tasks WHERE owner_id=$1
                ORDER BY created_at DESC LIMIT $2""", uid, lim)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_mesh_tasks WHERE owner_id=$1", uid)
        names = await _mesh_bot_names(uid)
        out = []
        for r in (rows or []):
            d = dict(r)
            route = d.get("route") or []
            if isinstance(route, str):
                try:
                    route = json.loads(route)
                except (TypeError, ValueError):
                    route = []
            trace = d.get("trace") or []
            if isinstance(trace, str):
                try:
                    trace = json.loads(trace)
                except (TypeError, ValueError):
                    trace = []
            out.append({
                "task_id": d["task_id"],
                "origin_bot": d.get("origin_bot"),
                "origin_name": names.get(d.get("origin_bot")),
                # Маршрут — именами ботов: идентификатор владельцу ничего не говорит.
                "route": [{"bot": st.get("bot"),
                           "bot_name": names.get(st.get("bot")),
                           "capability": st.get("capability")}
                          for st in route if isinstance(st, dict)],
                "step": d.get("step") or 0,
                "depth": d.get("depth") or 0,
                "trace_names": [names.get(b) or ("бот #" + str(b)) for b in trace],
                "status": d.get("status") or "running",
                "drop_reason": d.get("drop_reason"),
                "deadline_at": d["deadline_at"].isoformat() if d.get("deadline_at") else None,
                "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
                "updated_at": d["updated_at"].isoformat() if d.get("updated_at") else None,
            })
        return _json_resp({"tasks": out, "total": total})

    async def _mesh_bot_names(uid: int) -> dict:
        """bot_id → @username владельца. Экран показывает имена, а не числа."""
        rows = await _safe_fetch(pool,
            "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1", uid)
        out = {}
        for r in (rows or []):
            out[r["bot_id"]] = ("@" + r["username"]) if r["username"] else (
                r["first_name"] or None)
        return out

    async def mesh_task_detail(request: web.Request) -> web.Response:
        """Трасса одной задачи: кто кому передал, что вышло и почему брошено."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        task_id = request.match_info.get("task_id") or ""
        task = await _safe_fetchrow(pool,
            """SELECT task_id, origin_bot, route, step, depth, trace, payload,
                      status, drop_reason, deadline_at, created_at, updated_at
                 FROM bot_mesh_tasks WHERE task_id=$1 AND owner_id=$2""",
            task_id, uid)
        if not task:
            return _err("Задача не найдена", 404)
        hops = await _safe_fetch(pool,
            """SELECT step, from_bot, to_bot, capability, outcome, reason, created_at
                 FROM bot_mesh_hops WHERE task_id=$1 ORDER BY created_at LIMIT 200""",
            task_id)
        names = await _mesh_bot_names(uid)
        d = dict(task)
        return _json_resp({
            "task_id": d["task_id"],
            "status": d.get("status") or "running",
            "drop_reason": d.get("drop_reason"),
            "step": d.get("step") or 0,
            "depth": d.get("depth") or 0,
            "origin_name": names.get(d.get("origin_bot")),
            "deadline_at": d["deadline_at"].isoformat() if d.get("deadline_at") else None,
            "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
            "hops": [{
                "step": h["step"],
                "from_name": names.get(h["from_bot"]) or (
                    ("бот #" + str(h["from_bot"])) if h["from_bot"] else "запуск"),
                "to_name": names.get(h["to_bot"]) or (
                    ("бот #" + str(h["to_bot"])) if h["to_bot"] else "—"),
                "capability": h["capability"],
                "outcome": h["outcome"],
                "reason": h["reason"],
                "created_at": h["created_at"].isoformat() if h["created_at"] else None,
            } for h in (hops or [])],
        })

    async def parser_runs(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, source_type, source_ref, parse_type, status,
                          total_found, total_saved, started_at, finished_at, error
                   FROM parser_runs WHERE owner_id=$1
                    ORDER BY started_at DESC LIMIT $2""",
                uid, _list_limit(request, 30, 1000),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM parser_runs WHERE owner_id=$1", uid)
            # Плитка «N спарсено» суммировала показанную страницу: при 200
            # запусках она называла сумму последних тридцати. Сумму по всей
            # истории может посчитать только база.
            saved_total = await _safe_count(pool,
                "SELECT COALESCE(SUM(total_saved),0) FROM parser_runs "
                "WHERE owner_id=$1 AND status='done'", uid)
        except Exception:
            log.exception("parser_runs uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"total": total, "saved_total": saved_total, "runs": [
            {
                "id": r["id"],
                "source": r["source_ref"] or "",
                "source_type": r["source_type"] or "",
                "parse_type": r["parse_type"] or "",
                "status": r["status"] or "pending",
                "total_found": r["total_found"] or 0,
                "total_saved": r["total_saved"] or 0,
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "error": (r["error"] or "")[:300],
            }
            for r in rows
        ]})

    async def parsed_audience(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.rel_url.query
        # Потолок и сдвиг — через общие двери: `LIMIT -5` / `OFFSET -5` Postgres
        # не принимает, и раньше отрицательное число в параметрах давало
        # пятисотку вместо первой страницы.
        limit = _list_limit(request, 50, 200)
        offset = _list_offset(request)
        filt_sql, filt_params = parsed_audience_filters(q, base_params_count=1)
        try:
            rows = await pool.fetch(
                f"""SELECT tg_user_id, username, first_name, last_name, is_premium,
                           is_bot, is_active, phone, source_title, source_type, parsed_at
                    FROM parsed_audiences WHERE owner_id=$1{filt_sql}
                    ORDER BY parsed_at DESC, id LIMIT ${len(filt_params)+2} OFFSET ${len(filt_params)+3}""",
                uid, *filt_params, limit, offset,
            )
            total = await pool.fetchval(
                f"SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1{filt_sql}",
                uid, *filt_params,
            )
            # Счётчики срезов (по всей аудитории владельца, без учёта фильтров) —
            # чтобы показать «сколько premium / с телефоном / активных» есть вообще.
            slices = await pool.fetchrow(
                """SELECT COUNT(*) FILTER (WHERE is_premium) AS premium,
                          COUNT(*) FILTER (WHERE username IS NOT NULL AND username<>'') AS with_username,
                          COUNT(*) FILTER (WHERE phone IS NOT NULL AND phone<>'') AS with_phone,
                          COUNT(*) FILTER (WHERE is_active) AS active
                   FROM parsed_audiences WHERE owner_id=$1""",
                uid,
            )
        except Exception:
            log.exception("parsed_audience uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({
            "total": int(total or 0),
            "slices": {k: int(slices[k] or 0) for k in ("premium", "with_username", "with_phone", "active")} if slices else {},
            "users": [
                {
                    "tg_user_id": r["tg_user_id"],
                    "username": r["username"] or "",
                    "name": " ".join(filter(None, [r["first_name"], r["last_name"]])),
                    "is_premium": bool(r["is_premium"]),
                    "is_bot": bool(r["is_bot"]),
                    "is_active": bool(r["is_active"]),
                    "has_phone": bool(r["phone"]),
                    "source_title": r["source_title"] or "",
                    "source_type": r["source_type"] or "",
                    "parsed_at": r["parsed_at"].isoformat() if r["parsed_at"] else None,
                }
                for r in rows
            ],
        })

    async def parsed_audience_export(request: web.Request) -> web.Response:
        """Экспорт аудитории с теми же фильтрами, что и просмотр.
        ?format=csv|txt|json (по умолчанию csv). TXT — @username/id построчно
        (готово для инвайтера), JSON — полные объекты. Паритет Telegram Expert."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.rel_url.query
        fmt = (q.get("format") or "csv").strip().lower()
        if fmt not in ("csv", "txt", "json"):
            fmt = "csv"
        filt_sql, filt_params = parsed_audience_filters(q, base_params_count=1)
        try:
            rows = await pool.fetch(
                f"""SELECT tg_user_id, username, first_name, last_name, phone,
                           is_premium, is_bot, is_active, source_title, parsed_at
                    FROM parsed_audiences WHERE owner_id=$1{filt_sql}
                    ORDER BY parsed_at DESC LIMIT 50000""",
                uid, *filt_params,
            )
        except Exception:
            log.exception("parsed_audience_export uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        if fmt == "csv":
            header = ["tg_user_id", "username", "first_name", "last_name", "phone",
                      "is_premium", "is_bot", "is_active", "source", "parsed_at"]
            data = [
                [r["tg_user_id"], r["username"] or "", r["first_name"] or "", r["last_name"] or "",
                 r["phone"] or "", r["is_premium"], r["is_bot"], r["is_active"],
                 r["source_title"] or "", r["parsed_at"].isoformat() if r["parsed_at"] else ""]
                for r in rows
            ]
            return _csv_resp("parsed_audience.csv", header, data)
        # txt / json — через чистые хелперы parser
        from services import parser as _parser
        users = [{
            "tg_user_id": r["tg_user_id"], "username": r["username"],
            "first_name": r["first_name"], "last_name": r["last_name"],
            "phone": r["phone"], "is_premium": r["is_premium"], "is_bot": r["is_bot"],
            "is_active": r["is_active"], "source_title": r["source_title"],
            "parsed_at": r["parsed_at"].isoformat() if r["parsed_at"] else "",
        } for r in rows]
        if fmt == "txt":
            body, ctype, fname = _parser.audience_to_txt(users), "text/plain", "parsed_audience.txt"
        else:
            body, ctype, fname = _parser.audience_to_json(users), "application/json", "parsed_audience.json"
        # Без Access-Control-Allow-Origin: в выгрузке телефоны и имена, а
        # звёздочка разрешала любому чужому сайту прочитать её ответом на
        # запрос с токеном. Мини-апп берёт файл со своего же адреса.
        return web.Response(text=body, content_type=ctype, charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    async def submit_parse_job(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            # Нормализуем ссылку/username: пользователи вставляют https://t.me/name,
            # t.me/name?after=…, @name — раньше .lstrip("@") оставлял URL как есть и
            # источник не резолвился.
            from services import parser as _parser
            source_ref = _parser.normalize_source_ref(str(body.get("source_ref", "")))
            parse_type = str(body.get("parse_type", "members")).strip()
            limit = int(body.get("limit", 500))
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not source_ref:
            return _err("source_ref обязателен", 400)
        if parse_type not in ("members", "active", "comments"):
            parse_type = "members"
        if limit < 1 or limit > 10000:
            limit = 500
        # days_back — окно активности для режима "active" (бэкенд умел, UI хардкодил 30).
        try:
            days_back = max(1, min(365, int(body.get("days_back") or 30)))
        except (TypeError, ValueError):
            days_back = 30
        _win = f", {days_back}д" if parse_type in ("active", "comments") else ""
        _kind = _parser.PARSE_TYPE_LABELS.get(parse_type, parse_type)
        label = f"Разбор {_kind} из @{source_ref} (до {limit}{_win})"
        try:
            op_id = await _obus.submit(
                pool, uid, "parse_audience",
                {"source_ref": source_ref, "parse_type": parse_type,
                 "limit": limit, "days_back": days_back},
                total_items=limit, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("submit_parse_job uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def topology_nodes(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            # Каналы берём из managed_channels — это таблица, куда реально пишутся
            # каналы пользователя (8 путей записи). Прежний источник tg_channels
            # НИКЕМ не заполняется (нет ни одного INSERT/UPDATE в коде и сидов в
            # миграциях) → узлы-каналы в топологии всегда были пусты. DISTINCT ON
            # (channel_id): один канал может вестись несколькими аккаунтами (строка
            # на acc_id) — иначе в графе дубли-узлы. Активность по факту наличия.
            channels = await pool.fetch(
                "SELECT DISTINCT ON (channel_id) channel_id AS id, username, title "
                "FROM managed_channels WHERE owner_id=$1 ORDER BY channel_id LIMIT 50",
                uid,
            )
            # managed_bots скоупится по added_by, НЕ owner_id (такой колонки нет) —
            # прежний owner_id=$1 валил эндпоинт 500.
            bots = await pool.fetch(
                "SELECT bot_id, username, first_name, is_active FROM managed_bots WHERE added_by=$1 LIMIT 50", uid
            )
            nodes = []
            for ch in channels:
                nodes.append({"id": f"ch_{ch['id']}", "type": "channel", "name": ch['username'] or ch['title'] or f"#{ch['id']}", "active": True})
            for b in bots:
                nodes.append({"id": f"bot_{b['bot_id']}", "type": "bot", "name": f"@{b['username']}" if b['username'] else b['first_name'] or f"#{b['bot_id']}", "active": b['is_active']})
            return _json_resp({"nodes": nodes})
        except Exception:
            log.exception("topology_nodes")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/organism/pulse", organism_pulse)
    app.router.add_get("/api/miniapp/organism/digest", organism_digest)
    app.router.add_post("/api/miniapp/organism/dismiss", organism_dismiss)
    app.router.add_post("/api/miniapp/organism/pending_open", organism_pending_open)
    app.router.add_get("/api/miniapp/parser/runs", parser_runs)
    app.router.add_get("/api/miniapp/parser/audience", parsed_audience)
    app.router.add_get("/api/miniapp/parser/audience/export", parsed_audience_export)
    app.router.add_post("/api/miniapp/parser/submit", submit_parse_job)
    app.router.add_get("/api/miniapp/mesh/tasks", mesh_tasks)
    app.router.add_post("/api/miniapp/mesh/task", mesh_task_create)
    app.router.add_post("/api/miniapp/mesh/task/{task_id}/drop", mesh_task_drop)
    app.router.add_get("/api/miniapp/mesh/task/{task_id}", mesh_task_detail)
    app.router.add_get("/api/miniapp/topology", topology_overview)
    app.router.add_get("/api/miniapp/topology/nodes", topology_nodes)
    app.router.add_get("/api/miniapp/topology/links", topology_links)
