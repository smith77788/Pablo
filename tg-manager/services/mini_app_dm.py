"""Кампании личных сообщений — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: семь маршрутов
`/api/miniapp/dm_campaign*` и семь функций — создание, правка, запуск, пауза,
отчёт, журнал, удаление.

Рассылка в личные — самая баноопасная операция после инвайта, и отвечает за
неё не этот файл: темп, лимиты и карантин аккаунтов живут в
`services/op_worker.py` и `services/dm_*`, постановка — в
`services/operation_bus.py`. Здесь только разбор запроса, скоуп владельца и
русские ошибки. Поведение при переносе не менялось: тела обработчиков и строки
регистрации маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты кампаний личных сообщений на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json, _json_resp, _list_limit,
        _obus, _safe_count, _safe_fetch, _safe_fetchrow, log,
    )
    from services.security import (
        check_sql_suspicious, is_safe_public_url, validate_string,
    )

    async def create_dm_campaign(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        name = validate_string(body.get("name"), max_len=200)
        text_template = validate_string(body.get("text_template"), max_len=4096)
        target_type = validate_string(body.get("target_type"), max_len=30) or "all_bots"
        target_id = body.get("target_id")
        if not name:
            return _err("Нужно название")
        if not text_template:
            return _err("Нужен шаблон текста")
        if check_sql_suspicious(text_template):
            return _err("Недопустимые символы в тексте сообщения")
        if target_type not in ("bot_users", "all_bots", "crm", "parsed_audience"):
            return _err("Неверный тип цели")
        if target_type == "bot_users" and not target_id:
            return _err("Для цели bot_users нужен идентификатор цели")
        total_targets = 0
        if target_type == "bot_users" and target_id:
            try:
                bot_id_int = int(target_id)
            except (TypeError, ValueError):
                return _err("Неверный идентификатор цели", 400)
            # IDOR-защита: бот должен принадлежать пользователю, иначе можно
            # разослать DM подписчикам чужого бота и узнать их число.
            owns_bot = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id_int, uid)
            if not owns_bot:
                return _err("Бот не найден", 404)
            total_targets = await _safe_count(pool,
                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true "
                "AND is_blocked=false AND suspect=false", bot_id_int)
        elif target_type == "all_bots":
            total_targets = await _safe_count(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=true
                     AND bu.is_blocked=false AND bu.suspect=false""", uid)
        try:
            row = await pool.fetchrow(
                """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, status, total_targets)
                   VALUES($1,$2,$3,$4,$5,'draft',$6) RETURNING id""",
                uid, name, text_template, target_type,
                int(target_id) if target_id else None, total_targets)
            return _json_resp({"ok": True, "id": row["id"], "total_targets": total_targets})
        except Exception:
            log.exception("create_dm_campaign uid=%d", uid)
            return _err("Не удалось создать кампанию", 500)

    async def dm_campaign_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, name, status FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
                campaign_id, uid,
            )
        except Exception:
            log.exception("dm_campaign_launch fetch uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Не найдено", 404)
        if row["status"] == "running":
            # Статус 'running' мог залипнуть: операция упала с исключением или
            # контейнер перезапустился, а строка кампании осталась в работе.
            # Тогда запуск отвечал 409 навсегда — кампанию было не спасти, только
            # удалить. Считаем её идущей, ТОЛЬКО если под неё есть живая операция.
            _alive = await _safe_count(
                pool,
                """SELECT COUNT(*) FROM operation_queue
                   WHERE owner_id=$1 AND op_type='dm_campaign'
                     AND status IN ('pending','running')
                     AND (params->>'campaign_id') = $2""",
                uid, str(campaign_id),
            )
            if _alive:
                return _err("Кампания уже выполняется", 409)
            log.warning(
                "dm_campaign_launch: кампания %d висела в 'running' без операции — перезапускаем",
                campaign_id,
            )
        try:
            label = f"DM-кампания: {row['name']}"
            op_id = await _obus.submit(
                pool, uid, "dm_campaign", {"campaign_id": campaign_id},
                total_items=1, label=label)
            await pool.execute(
                "UPDATE dm_campaigns SET status='running', started_at=now() WHERE id=$1 AND owner_id=$2", campaign_id, uid
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("dm_campaign_launch uid=%d cid=%d", uid, campaign_id)
            return _err(_INTERNAL_ERROR, 500)

    async def dm_campaign_update(request: web.Request) -> web.Response:
        """Отредактировать неидущую кампанию: название, текст и настройки темпа.

        Эндпоинта правки не было вовсе: опечатка в тексте означала удалить
        кампанию и собрать заново — заново выбрать аудиторию, темп, лимит на
        аккаунт, медиа. Аудиторию здесь СОЗНАТЕЛЬНО не меняем: часть получателей
        уже может быть обработана, и смена аудитории на полпути превратила бы
        журнал отправок в кашу — это отдельная кампания.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        row = await _safe_fetchrow(
            pool,
            "SELECT status, params FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
            campaign_id, uid,
        )
        if not row:
            return _err("Не найдено", 404)
        if row["status"] == "running":
            return _err("Нельзя править идущую кампанию — сначала поставьте на паузу", 409)

        sets: list[str] = []
        args: list = []

        if body.get("name") is not None:
            name = (body.get("name") or "").strip()
            if not name:
                return _err("Название не может быть пустым", 400)
            args.append(name[:100])
            sets.append(f"name=${len(args)}")

        if body.get("text") is not None:
            text = validate_string(body.get("text"), max_len=4096)
            if not text:
                return _err("Текст не может быть пустым", 400)
            if check_sql_suspicious(text):
                return _err("Недопустимые символы в тексте", 400)
            args.append(text)
            sets.append(f"text_template=${len(args)}")

        # Настройки живут в params — читаем текущие и правим точечно, чтобы не
        # затереть то, чего нет в запросе (медиа, список получателей, когорту).
        _p = row["params"] or {}
        if isinstance(_p, str):
            try:
                _p = _json.loads(_p)
            except Exception:
                _p = {}
        if not isinstance(_p, dict):
            _p = {}
        _params_touched = False

        if body.get("pace") is not None:
            pace = (body.get("pace") or "normal").strip()
            if pace not in ("slow", "normal", "fast", "auto"):
                return _err("Неизвестный темп", 400)
            _p["pace"] = pace
            _params_touched = True

        if "per_account_daily" in body:
            raw = body.get("per_account_daily")
            if raw in (None, "", 0):
                _p.pop("per_account_daily", None)
            else:
                try:
                    _p["per_account_daily"] = max(1, min(200, int(raw)))
                except (TypeError, ValueError):
                    return _err("per_account_daily должен быть числом", 400)
            _params_touched = True

        if "quiet_hours" in body:
            if body.get("quiet_hours") is False:
                _p["quiet_hours"] = False
            else:
                _p.pop("quiet_hours", None)  # умолчание живёт в движке
            _params_touched = True

        if "media_url" in body:
            media_url = (body.get("media_url") or "").strip()
            if media_url:
                if not is_safe_public_url(media_url):
                    return _err("Нужен публичный https URL медиа", 400)
                _p["media_url"] = media_url
            else:
                _p.pop("media_url", None)
            _params_touched = True

        if "name_fallback" in body:
            _nf = (body.get("name_fallback") or "").strip()
            if _nf:
                _p["name_fallback"] = _nf[:32]
            else:
                _p.pop("name_fallback", None)   # вернуться к умолчанию движка
            _params_touched = True

        if _params_touched:
            args.append(_json.dumps(_p))
            sets.append(f"params=${len(args)}::jsonb")

        if not sets:
            return _err("Нечего изменять", 400)

        args.extend([campaign_id, uid])
        try:
            await pool.execute(
                f"UPDATE dm_campaigns SET {', '.join(sets)} "
                f"WHERE id=${len(args) - 1} AND owner_id=${len(args)}",
                *args,
            )
        except Exception:
            log.exception("dm_campaign_update uid=%d cid=%d", uid, campaign_id)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True})

    async def dm_campaign_report(request: web.Request) -> web.Response:
        """Отчёт по кампании: исходы, разбивка по аккаунтам, последние записи.

        dm_campaign_log писался с самого начала, но не читался НИ ОДНИМ экраном:
        пользователь видел только «✅ N ❌ M» и не мог узнать, кому не дошло,
        почему и какой аккаунт даёт сбои. Данные собирались и выбрасывались.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        # IDOR: отчёт только по своей кампании.
        row = await _safe_fetchrow(
            pool,
            "SELECT id, name, status, sent_count, fail_count, total_targets, target_type "
            "FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
            campaign_id, uid,
        )
        if not row:
            return _err("Не найдено", 404)

        by_status = await _safe_fetch(
            pool,
            "SELECT status, COUNT(*) AS n FROM dm_campaign_log "
            "WHERE campaign_id=$1 GROUP BY status ORDER BY n DESC",
            campaign_id,
        )
        # Разбивка по аккаунтам: так виден аккаунт, который сыпет ошибками,
        # пока остальные работают — иначе это тонет в общем счётчике.
        by_account = await _safe_fetch(
            pool,
            """SELECT l.account_id,
                      COALESCE(a.first_name, a.phone, 'Аккаунт ' || l.account_id) AS label,
                      COUNT(*) FILTER (WHERE l.status='sent')  AS sent,
                      COUNT(*) FILTER (WHERE l.status<>'sent') AS failed
                 FROM dm_campaign_log l
            LEFT JOIN tg_accounts a ON a.id = l.account_id
                WHERE l.campaign_id=$1
             GROUP BY l.account_id, a.first_name, a.phone
             ORDER BY sent DESC NULLS LAST
                LIMIT 50""",
            campaign_id,
        )
        # Частые причины недоставки — сразу подсказывают, что чинить.
        top_errors = await _safe_fetch(
            pool,
            "SELECT COALESCE(NULLIF(error_msg,''),'без описания') AS reason, COUNT(*) AS n "
            "FROM dm_campaign_log WHERE campaign_id=$1 AND status<>'sent' "
            "GROUP BY reason ORDER BY n DESC LIMIT 10",
            campaign_id,
        )
        # Имя получателя вместо голого id: «id 783215441» владельцу ничего не
        # говорит, а имя у нас почти всегда есть — цели берутся из CRM или из
        # спарсенной аудитории, и обе таблицы хранят имя и @username.
        recent = await _safe_fetch(
            pool,
            """SELECT l.tg_user_id, l.status, l.error_msg, l.sent_at,
                      COALESCE(c.first_name, p.first_name) AS name,
                      COALESCE(c.username, p.username)     AS username
                 FROM dm_campaign_log l
            LEFT JOIN LATERAL (SELECT first_name, username FROM crm_contacts
                                WHERE owner_id=$2 AND tg_user_id=l.tg_user_id
                                LIMIT 1) c ON TRUE
            LEFT JOIN LATERAL (SELECT first_name, username FROM parsed_audiences
                                WHERE owner_id=$2 AND tg_user_id=l.tg_user_id
                                LIMIT 1) p ON TRUE
                WHERE l.campaign_id=$1
             ORDER BY l.sent_at DESC
                LIMIT 100""",
            campaign_id, uid,
        )
        for r in recent:
            if r.get("sent_at") is not None:
                r["sent_at"] = r["sent_at"].isoformat()
        return _json_resp({
            "campaign": dict(row),
            "by_status": by_status,
            "by_account": by_account,
            "top_errors": top_errors,
            "recent": recent,
        })

    async def dm_campaign_log_list(request: web.Request) -> web.Response:
        """Кому именно не дошло — список получателей с фильтром по исходу/причине.

        Сводка отвечала «заблокировали: 54», но не «кто» — а это ровно тот
        вопрос, ради которого отчёт и открывают: вычистить мёртвые контакты,
        переслать другим каналом, понять, одна ли это аудитория.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        # IDOR: лог только по своей кампании.
        own = await _safe_fetchrow(
            pool,
            "SELECT name FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
            campaign_id, uid,
        )
        if not own:
            return _err("Не найдено", 404)
        status = validate_string(request.query.get("status"), 32, required=False) or None
        reason = validate_string(request.query.get("reason"), 200, required=False) or None
        limit = _list_limit(request, 100, 500)

        # Фильтры подставляются как параметры, а не склейкой условий: так в
        # обоих запросах (счётчик и список) одинаковое число аргументов и
        # неподставленный $N не разъедется при добавлении третьего фильтра.
        # 'без описания' — та же подпись, которой сводка называет пустую
        # причину, поэтому тап по ней фильтрует именно пустые error_msg.
        # «Не дошло» — это не один статус: доставка ломается blocked,
        # peer_flood, auth и retry. Поэтому отдельный фильтр, а не перечисление
        # статусов на стороне экрана, которое разъедется с движком.
        failed_only = request.query.get("failed") in ("1", "true", "yes")
        _f = ("($%d::text IS NULL OR l.status=$%d) AND "
              "($%d::text IS NULL OR COALESCE(NULLIF(l.error_msg,''),"
              "'без описания')=$%d) AND "
              "($%d::bool IS NOT TRUE OR l.status<>'sent')")

        total = await _safe_count(
            pool,
            "SELECT COUNT(*) FROM dm_campaign_log l WHERE l.campaign_id=$1 AND "
            + _f % (2, 2, 3, 3, 4),
            campaign_id, status, reason, failed_only,
        )
        rows = await _safe_fetch(
            pool,
            f"""SELECT l.tg_user_id, l.status, l.error_msg, l.sent_at,
                       COALESCE(c.first_name, p.first_name) AS name,
                       COALESCE(c.username, p.username)     AS username
                  FROM dm_campaign_log l
             LEFT JOIN LATERAL (SELECT first_name, username FROM crm_contacts
                                 WHERE owner_id=$2 AND tg_user_id=l.tg_user_id
                                 LIMIT 1) c ON TRUE
             LEFT JOIN LATERAL (SELECT first_name, username FROM parsed_audiences
                                 WHERE owner_id=$2 AND tg_user_id=l.tg_user_id
                                 LIMIT 1) p ON TRUE
                 WHERE l.campaign_id=$1 AND {_f % (3, 3, 4, 4, 5)}
              ORDER BY l.sent_at DESC
                 LIMIT {limit}""",
            campaign_id, uid, status, reason, failed_only,
        )
        for r in rows:
            if r.get("sent_at") is not None:
                r["sent_at"] = r["sent_at"].isoformat()
        return _json_resp({
            "campaign_name": own["name"],
            "items": rows,
            "total": total,
            "shown": len(rows),
        })

    async def dm_campaign_pause(request: web.Request) -> web.Response:
        """Поставить идущую кампанию на паузу.

        Движок проверяет статус кампании перед каждым получателем, поэтому
        смены статуса достаточно — цикл выйдет сам и кампанию можно будет
        возобновить с места остановки. Раньше кнопка «Возобновить» была, а
        поставить на паузу было нечем: остановить рассылку можно было только
        отменой операции в другом экране.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            row = await pool.fetchrow(
                "SELECT status FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
                campaign_id, uid,
            )
            if not row:
                return _err("Не найдено", 404)
            if row["status"] != "running":
                return _err("Пауза возможна только для идущей кампании", 409)
            await pool.execute(
                "UPDATE dm_campaigns SET status='paused' WHERE id=$1 AND owner_id=$2",
                campaign_id, uid,
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("dm_campaign_pause uid=%d cid=%d", uid, campaign_id)
            return _err(_INTERNAL_ERROR, 500)

    async def dm_campaign_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            await pool.execute(
                "DELETE FROM dm_campaigns WHERE id=$1 AND owner_id=$2", campaign_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("dm_campaign_delete uid=%d cid=%d", uid, campaign_id)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_post("/api/miniapp/dm_campaign", create_dm_campaign)
    app.router.add_post("/api/miniapp/dm_campaign/{campaign_id}/launch", dm_campaign_launch)
    app.router.add_post("/api/miniapp/dm_campaign/{campaign_id}/pause", dm_campaign_pause)
    app.router.add_get("/api/miniapp/dm_campaign/{campaign_id}/report", dm_campaign_report)
    app.router.add_get("/api/miniapp/dm_campaign/{campaign_id}/log", dm_campaign_log_list)
    app.router.add_patch("/api/miniapp/dm_campaign/{campaign_id}", dm_campaign_update)
    app.router.add_delete("/api/miniapp/dm_campaign/{campaign_id}", dm_campaign_delete)
