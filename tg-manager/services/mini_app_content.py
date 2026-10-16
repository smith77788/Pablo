"""Контент: сетка, нарратив, самопродвижение, клоны — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 30 маршрутов — контент-сетка
(`/content_mesh/*`: правила, цели, включение, удаление), нарративный движок,
самопродвижение, спинтакс, клонирование контента и его адаптация, ДНК
аудитории и семантическая память.

Вместе с ручками уехали `_own_mesh` (скоуп владельца для правила сетки) и
`_self_promo_audience` — других потребителей у них нет. `_spintax_pack` и
`_dna_to_dict` остались на месте: они модульные и чистые, их импортируем.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты контентных экранов на приложении мини-аппа."""
    from services import operation_bus as _obus
    from services.mini_app_api import (
        _INTERNAL_ERROR, _dna_to_dict, _err, _get_uid, _json_resp, _list_limit,
        _safe_count, _safe_fetch, _safe_fetchrow, _spintax_pack, log,
    )
    from services.security import validate_integer, validate_string

    # Отбор получателей самопиара. Условие слово в слово повторяет
    # op_worker._exec_self_promo_blast: число в подтверждении обязано совпадать
    # с тем, скольким людям сообщение реально уйдёт, иначе подтверждение врёт.
    # Расхождение стережёт tests/test_self_promo_names_the_scale.py.
    _SELF_PROMO_AUDIENCE_WHERE = (
        "FROM bot_users bu JOIN managed_bots mb ON mb.bot_id = bu.bot_id "
        "WHERE mb.added_by = $1 AND bu.is_active = TRUE AND mb.is_active = TRUE "
        "AND mb.token IS NOT NULL"
    )
    # Тот же потолок, что в LIMIT исполнителя: за один запуск больше не уйдёт.
    _SELF_PROMO_CAP = 1000

    async def clone_adapt_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT h.id, h.source_bot_id, h.target_bot_id, h.fields,
                       h.status, h.details, h.created_at,
                       sb.username AS source_uname, sb.first_name AS source_name,
                       tb.username AS target_uname, tb.first_name AS target_name
                FROM clone_adapt_history h
                LEFT JOIN managed_bots sb ON sb.bot_id = h.source_bot_id
                LEFT JOIN managed_bots tb ON tb.bot_id = h.target_bot_id
                WHERE h.owner_id=$1 ORDER BY h.created_at DESC LIMIT $2
                """,
                uid, _list_limit(request, 30, 1000),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM clone_adapt_history WHERE owner_id=$1", uid)
            # Было — голый массив; история обрывалась на 30 записях молча.
            return _json_resp({"items": [dict(r) for r in rows], "total": total})
        except Exception:
            log.exception("clone_adapt_history uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def content_mesh_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор задачи сети", 400)
        try:
            mesh = await pool.fetchrow(
                "SELECT enabled FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid
            )
            if not mesh:
                return _err("Не найдено", 404)
            new_state = not mesh["enabled"]
            await pool.execute(
                "UPDATE content_meshes SET enabled=$1, updated_at=NOW() WHERE id=$2 AND owner_id=$3", new_state, mesh_id, uid
            )
            return _json_resp({"enabled": new_state})
        except Exception:
            log.exception("content_mesh_toggle uid=%d mesh=%d", uid, mesh_id)
            return _err(_INTERNAL_ERROR, 500)

    async def clone_adapt_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        source_bot_id = data.get("source_bot_id")
        target_bot_id = data.get("target_bot_id")
        fields = data.get("fields", "name,desc")
        if not source_bot_id or not target_bot_id:
            return _err("source_bot_id и target_bot_id обязательны", 400)
        if int(source_bot_id) == int(target_bot_id):
            return _err("Источник и цель должны быть разными ботами", 400)
        try:
            src = await pool.fetchrow(
                "SELECT bot_id, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                int(source_bot_id), uid,
            )
            tgt = await pool.fetchrow(
                "SELECT bot_id, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                int(target_bot_id), uid,
            )
            if not src:
                return _err("Исходный бот не найден", 404)
            if not tgt:
                return _err("Целевой бот не найден", 404)
            src_name = src["username"] or src["first_name"] or f"id{src['bot_id']}"
            tgt_name = tgt["username"] or tgt["first_name"] or f"id{tgt['bot_id']}"
            op_id = await _obus.submit(
                pool, uid, "clone_adapt", {
                    "source_bot_id": int(source_bot_id),
                    "target_bot_id": int(target_bot_id),
                    "fields": fields,
                }, total_items=1, label=f"Клонирование: @{src_name} → @{tgt_name}")
            return _json_resp({"op_id": op_id, "ok": True})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("clone_adapt_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def content_mesh_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = str(data.get("name", "")).strip()
        source_channel = str(data.get("source_channel", "")).strip() or None
        source_account_id = data.get("source_account_id")
        delay_minutes = int(data.get("delay_minutes", 30))
        append_text = str(data.get("append_text", "")).strip() or None
        if not name:
            return _err("name обязателен", 400)
        if delay_minutes < 1:
            delay_minutes = 1
        try:
            mid = await pool.fetchval(
                """INSERT INTO content_meshes
                   (owner_id, name, source_channel, source_account_id, delay_minutes, append_text)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                uid, name, source_channel,
                int(source_account_id) if source_account_id else None,
                delay_minutes, append_text,
            )
            return _json_resp({"id": mid, "ok": True})
        except Exception:
            log.exception("content_mesh_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _own_mesh(uid: int, mesh_id: int):
        return await _safe_fetchrow(pool,
            "SELECT * FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid)

    async def content_mesh_detail(request: web.Request) -> web.Response:
        """Сеть целиком: настройки, цели и очередь репостов.

        mesh_queue с самого начала хранит, что ушло, что ждёт и что упало с
        текстом ошибки. Экран показывал из этого одно число «В очереди», и
        почему репост не дошёл, узнать было негде.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор сети", 400)
        mesh = await _own_mesh(uid, mesh_id)
        if not mesh:
            return _err("Сеть не найдена", 404)
        try:
            targets = await _safe_fetch(pool,
                "SELECT id, target_channel, enabled, added_at FROM mesh_targets "
                "WHERE mesh_id=$1 ORDER BY id", mesh_id)
            counts = await _safe_fetch(pool,
                "SELECT status, COUNT(*) AS cnt FROM mesh_queue WHERE mesh_id=$1 "
                "GROUP BY status", mesh_id)
            recent = await _safe_fetch(pool,
                """SELECT q.id, q.source_msg_id, q.status, q.scheduled_at, q.sent_at,
                          q.error_msg, t.target_channel
                     FROM mesh_queue q
                     LEFT JOIN mesh_targets t ON t.id = q.target_id
                    WHERE q.mesh_id=$1
                 ORDER BY COALESCE(q.sent_at, q.scheduled_at) DESC LIMIT $2""",
                mesh_id, _list_limit(request, 30, 200))
            m = dict(mesh)
            for key in ("created_at", "updated_at"):
                if m.get(key):
                    m[key] = m[key].isoformat()
            return _json_resp({
                "mesh": m,
                "targets": [dict(t) for t in (targets or [])],
                "queue": {r["status"]: int(r["cnt"]) for r in (counts or [])},
                "recent": [{
                    "id": r["id"], "source_msg_id": r["source_msg_id"],
                    "status": r["status"], "target_channel": r["target_channel"],
                    "error_msg": r["error_msg"],
                    "scheduled_at": r["scheduled_at"].isoformat() if r["scheduled_at"] else None,
                    "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
                } for r in (recent or [])],
            })
        except Exception:
            log.exception("content_mesh_detail uid=%d mesh=%d", uid, mesh_id)
            return _err(_INTERNAL_ERROR, 500)

    async def content_mesh_update(request: web.Request) -> web.Response:
        """Поменять настройки сети. Раньше их нельзя было поправить вообще."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор сети", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = validate_string(body.get("name"), max_len=120)
        if not name:
            return _err("Название сети обязательно", 400)
        delay = validate_integer(body.get("delay_minutes", 30), min_val=1, max_val=1440)
        if delay is None:
            return _err("Задержка репоста — от 1 до 1440 минут", 400)
        append_text = validate_string(body.get("append_text") or "", max_len=512,
                                      required=False) or None
        if not await _own_mesh(uid, mesh_id):
            return _err("Сеть не найдена", 404)
        try:
            await pool.execute(
                "UPDATE content_meshes SET name=$3, delay_minutes=$4, append_text=$5, "
                "updated_at=now() WHERE id=$1 AND owner_id=$2",
                mesh_id, uid, name, delay, append_text)
            return _json_resp({"ok": True, "name": name, "delay_minutes": delay})
        except Exception:
            log.exception("content_mesh_update uid=%d mesh=%d", uid, mesh_id)
            return _err(_INTERNAL_ERROR, 500)

    async def content_mesh_delete(request: web.Request) -> web.Response:
        """Удалить сеть. Её можно было только выключить — список копился."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор сети", 400)
        try:
            gone = await _safe_fetchrow(pool,
                "DELETE FROM content_meshes WHERE id=$1 AND owner_id=$2 RETURNING id",
                mesh_id, uid)
            if not gone:
                return _err("Сеть не найдена", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("content_mesh_delete uid=%d mesh=%d", uid, mesh_id)
            return _err(_INTERNAL_ERROR, 500)

    async def content_mesh_target_toggle(request: web.Request) -> web.Response:
        """Приостановить одну цель, не трогая всю сеть."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            target_id = int(request.match_info["target_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор цели", 400)
        try:
            # Скоуп через владельца сети: по голому id цели можно было бы
            # выключить чужую.
            row = await _safe_fetchrow(pool,
                """SELECT mt.id, mt.enabled FROM mesh_targets mt
                     JOIN content_meshes cm ON cm.id = mt.mesh_id
                    WHERE mt.id=$1 AND cm.owner_id=$2""", target_id, uid)
            if not row:
                return _err("Цель не найдена", 404)
            new_val = not row["enabled"]
            await pool.execute("UPDATE mesh_targets SET enabled=$2 WHERE id=$1",
                               target_id, new_val)
            return _json_resp({"ok": True, "enabled": new_val})
        except Exception:
            log.exception("content_mesh_target_toggle uid=%d target=%d", uid, target_id)
            return _err(_INTERNAL_ERROR, 500)

    async def content_mesh_targets_list(request: web.Request) -> web.Response:
        """Список целевых каналов меша. Без целей меш ничего не репостит (runner
        читает mesh_targets) — в mini-app их раньше нельзя было задать вообще."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор задачи сети", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid)
        if not owns:
            return _err("Меш не найден", 404)
        rows = await _safe_fetch(pool,
            "SELECT id, target_channel, enabled, added_at FROM mesh_targets WHERE mesh_id=$1 ORDER BY id", mesh_id)
        return _json_resp({"targets": [dict(r) for r in rows]})

    async def content_mesh_target_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid)
        if not owns:
            return _err("Меш не найден", 404)
        target = (body.get("target_channel") or "").strip()
        if not target:
            return _err("Укажите канал-цель (@username или -100…)", 400)
        if len(target) > 128:
            return _err("Слишком длинный идентификатор", 400)
        cnt = await _safe_count(pool, "SELECT COUNT(*) FROM mesh_targets WHERE mesh_id=$1", mesh_id)
        if cnt >= 200:
            return _err("Достигнут лимит целей (200)", 400)
        try:
            # Идемпотентно: повтор включает существующую цель, а не плодит дубли.
            existing = await pool.fetchrow(
                "SELECT id FROM mesh_targets WHERE mesh_id=$1 AND target_channel=$2", mesh_id, target)
            if existing:
                await pool.execute("UPDATE mesh_targets SET enabled=TRUE WHERE id=$1", existing["id"])
                return _json_resp({"ok": True, "id": existing["id"], "reactivated": True})
            row = await pool.fetchrow(
                "INSERT INTO mesh_targets(mesh_id, target_channel) VALUES($1,$2) RETURNING id",
                mesh_id, target)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("content_mesh_target_add mesh=%d uid=%d", mesh_id, uid)
            return _err("Не удалось добавить цель", 500)

    async def content_mesh_target_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            target_id = int(request.match_info["target_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор цели", 400)
        # Владение через JOIN на content_meshes.
        row = await _safe_fetchrow(pool,
            """SELECT mt.id FROM mesh_targets mt
               JOIN content_meshes cm ON cm.id=mt.mesh_id
               WHERE mt.id=$1 AND cm.owner_id=$2""", target_id, uid)
        if not row:
            return _err("Цель не найдена", 404)
        try:
            await pool.execute("DELETE FROM mesh_targets WHERE id=$1", target_id)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("content_mesh_target_delete target=%d uid=%d", target_id, uid)
            return _err("Не удалось удалить цель", 500)

    async def narrative_campaigns_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, topic, campaign_type, spread_hours, posts_total, posts_published, status, created_at "
                "FROM narrative_campaigns WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
                uid, _list_limit(request, 30, 1000),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM narrative_campaigns WHERE owner_id=$1", uid)
            # Было — голый массив; плитка «N кампаний» считала показанное, а не всё.
            return _json_resp({"items": [dict(r) for r in rows], "total": total})
        except Exception:
            log.exception("narrative_campaigns_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def narrative_campaign_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            campaign = await pool.fetchrow(
                "SELECT * FROM narrative_campaigns WHERE id=$1 AND owner_id=$2", cid, uid
            )
            if not campaign:
                return _err("Не найдено", 404)
            posts = await pool.fetch(
                "SELECT channel_username, angle, status, scheduled_at, published_at "
                "FROM narrative_posts WHERE campaign_id=$1 ORDER BY scheduled_at LIMIT 50",
                cid,
            )
            return _json_resp({"campaign": dict(campaign), "posts": [dict(p) for p in posts]})
        except Exception:
            log.exception("narrative_campaign_detail uid=%d cid=%d", uid, cid)
            return _err(_INTERNAL_ERROR, 500)

    async def narrative_campaign_pause(request: web.Request) -> web.Response:
        """Пауза нарратив-кампании (движок умел, но UI/роут не выводили)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            from services import narrative_engine
            ok = await narrative_engine.pause_campaign(pool, cid, uid)
            if not ok:
                return _err("Кампанию нельзя приостановить (не найдена/не активна)", 404)
            return _json_resp({"ok": True, "status": "paused"})
        except Exception:
            log.exception("narrative_campaign_pause uid=%d cid=%d", uid, cid)
            return _err(_INTERNAL_ERROR, 500)

    async def narrative_campaign_resume(request: web.Request) -> web.Response:
        """Возобновить приостановленную нарратив-кампанию."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        try:
            from services import narrative_engine
            ok = await narrative_engine.resume_campaign(pool, cid, uid)
            if not ok:
                return _err("Кампанию нельзя возобновить (не найдена/не на паузе)", 404)
            return _json_resp({"ok": True, "status": "active"})
        except Exception:
            log.exception("narrative_campaign_resume uid=%d cid=%d", uid, cid)
            return _err(_INTERNAL_ERROR, 500)

    async def narrative_campaign_create(request: web.Request) -> web.Response:
        """Создаёт и сразу запускает кампанию (как мастер в боте) — а не пустой
        черновик: без выбранных каналов посты некому публиковать, и раньше
        кампания, созданная тут, навсегда оставалась 'draft' с 0 постов."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        topic = str(data.get("topic", "")).strip()
        core_message = str(data.get("core_message", "")).strip()
        campaign_type = data.get("campaign_type", "trend")
        spread_hours = int(data.get("spread_hours", 4))
        channel_ids = data.get("channel_ids") or []
        if not topic or not core_message:
            return _err("topic и core_message обязательны", 400)
        if not channel_ids:
            return _err("Выберите хотя бы один канал", 400)
        if campaign_type not in ("trend", "launch", "awareness", "counter"):
            campaign_type = "trend"
        if spread_hours < 1:
            spread_hours = 1
        try:
            req_ids = [int(x) for x in channel_ids if str(x).lstrip("-").isdigit()]
        except (TypeError, ValueError):
            req_ids = []
        rows = await _safe_fetch(pool,
            "SELECT channel_id, acc_id, username, title FROM managed_channels "
            "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])", uid, req_ids)
        if not rows:
            return _err("Каналы не найдены", 404)

        from services.ai_providers import configured_providers
        from services import narrative_engine
        providers = configured_providers()
        ai_provider = providers[0] if providers else None
        if ai_provider is None:
            return _err("AI-провайдер не настроен — добавьте API-ключ в Настройках", 400)

        channel_usernames = [r["username"] or str(r["channel_id"]) for r in rows]
        channel_meta = [{"channel_id": r["channel_id"], "acc_id": r["acc_id"]} for r in rows]
        try:
            cid = await narrative_engine.create_campaign(
                pool=pool,
                owner_id=uid,
                topic=topic,
                core_message=core_message,
                channel_usernames=channel_usernames,
                channel_meta=channel_meta,
                spread_hours=spread_hours,
                campaign_type=campaign_type,
                ai_provider=ai_provider,
            )
            return _json_resp({"id": cid, "ok": True})
        except Exception:
            log.exception("narrative_campaign_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def spintax_generate(request: web.Request) -> web.Response:
        """Обычный текст → spintax-шаблоны (через LLM) + пример раскрытия."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        script = str(data.get("script", "")).strip()
        if not script:
            return _err("Пришлите текст сценария", 400)
        if len(script) > 4000:
            return _err("Слишком длинный текст (лимит 4000 символов)", 400)

        from services import spintax_ai, spintax_service

        try:
            templates = await spintax_service.generate_spins(
                script,
                complete=spintax_ai.complete,
                count=spintax_service.DEFAULT_SPIN_COUNT,
            )
        except spintax_service.SpintaxServiceError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("spintax_generate uid=%d", uid)
            return _err("Внутренняя ошибка генерации", 500)
        return _json_resp({"variants": _spintax_pack(templates)})

    async def spintax_expand(request: web.Request) -> web.Response:
        """Быстрая перегенерация: раскрыть готовые шаблоны заново, без LLM."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        templates = data.get("templates") or []
        single = str(data.get("template", "")).strip()
        if single:
            templates = [single]
        templates = [str(t).strip() for t in templates if str(t).strip()]
        if not templates:
            return _err("Нет шаблонов для раскрытия", 400)

        from services import spintax_service

        for tpl in templates:
            if not spintax_service.is_valid_template(tpl):
                return _err("Некорректный spintax-шаблон", 400)
        # expand = «Ещё генерация» → показываем случайную комбинацию
        return _json_resp({"variants": _spintax_pack(templates, random_sample=True)})

    async def self_promo_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, style, title, content, cta_text, cta_url, add_referral, is_active, use_count, "
                "CASE WHEN owner_id=$1 THEN true ELSE false END AS is_mine "
                "FROM self_promo_templates WHERE owner_id=$1 OR owner_id IS NULL ORDER BY id",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("self_promo_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def self_promo_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        style = data.get("style", "direct")
        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        cta_text = str(data.get("cta_text", "")).strip() or None
        cta_url = str(data.get("cta_url", "")).strip() or None
        add_referral = bool(data.get("add_referral", False))
        if not title or not content:
            return _err("title и content обязательны", 400)
        if style not in ("direct", "native"):
            style = "direct"
        if len(content) > 4096:
            return _err("content слишком длинный (макс 4096 символов)", 400)
        try:
            tid = await pool.fetchval(
                """INSERT INTO self_promo_templates
                   (owner_id, style, title, content, cta_text, cta_url, add_referral)
                   VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                uid, style, title, content, cta_text, cta_url, add_referral,
            )
            return _json_resp({"id": tid, "ok": True})
        except Exception:
            log.exception("self_promo_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def self_promo_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шаблона", 400)
        try:
            result = await pool.execute(
                "DELETE FROM self_promo_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
            )
            if result == "DELETE 0":
                return _err("Шаблон не найден или нельзя удалить системный шаблон", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("self_promo_delete uid=%d tpl=%d", uid, tpl_id)
            return _err(_INTERNAL_ERROR, 500)

    async def self_promo_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шаблона", 400)
        try:
            tpl = await pool.fetchrow(
                "SELECT is_active, owner_id FROM self_promo_templates WHERE id=$1 AND (owner_id=$2 OR owner_id IS NULL)",
                tpl_id, uid,
            )
            if not tpl:
                return _err("Не найдено", 404)
            # Системные (общие) шаблоны нельзя переключать обычному пользователю —
            # это влияло бы на всех. Меняем только свои.
            if tpl["owner_id"] is None:
                return _err("Системный шаблон нельзя переключать", 403)
            new_state = not tpl["is_active"]
            await pool.execute(
                "UPDATE self_promo_templates SET is_active=$1 WHERE id=$2 AND owner_id=$3",
                new_state, tpl_id, uid,
            )
            return _json_resp({"active": new_state})
        except Exception:
            log.exception("self_promo_toggle uid=%d tpl=%d", uid, tpl_id)
            return _err(_INTERNAL_ERROR, 500)

    async def _self_promo_audience(uid: int) -> dict:
        row = await _safe_fetchrow(pool,
            "SELECT COUNT(*) AS people, COUNT(DISTINCT bu.bot_id) AS bots "
            + _SELF_PROMO_AUDIENCE_WHERE, uid)
        people = int(row["people"] or 0) if row else 0
        return {
            "people": people,
            "bots": int(row["bots"] or 0) if row else 0,
            "cap": _SELF_PROMO_CAP,
            "will_send": min(people, _SELF_PROMO_CAP),
        }

    async def self_promo_audience(request: web.Request) -> web.Response:
        """Скольким людям уйдёт самопиар — чтобы спросить до отправки.

        Рассылка в личные сообщения необратима и банооопасна, а подтверждение
        спрашивало «запустить рассылку?» не называя ни одного числа. Размер
        аудитории исполнитель считает сам, но уже ПОСЛЕ запуска.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            return _json_resp(await _self_promo_audience(uid))
        except Exception:
            log.exception("self_promo_audience uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def self_promo_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шаблона", 400)
        try:
            tpl = await pool.fetchrow(
                "SELECT id, title FROM self_promo_templates "
                "WHERE id=$1 AND is_active AND (owner_id=$2 OR owner_id IS NULL)",
                tpl_id, uid,
            )
            if not tpl:
                return _err("Шаблон не найден или неактивен", 404)
            # total_items=1 означало, что в очереди операция до самого старта
            # показывала «1 из 1» независимо от того, уходит она троим или
            # тысяче: исполнитель поправляет число, только когда до неё дойдут
            # руки воркера. Ставим настоящий размер сразу.
            aud = await _self_promo_audience(uid)
            op_id = await _obus.submit(
                pool, uid, "self_promo_blast", {"template_id": tpl_id},
                total_items=max(1, int(aud["will_send"])),
                label=f"Самопиар: {tpl['title'] or tpl_id}")
            return _json_resp({"ok": True, "op_id": op_id,
                               "recipients": aud["will_send"]})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("self_promo_launch uid=%d tpl=%d", uid, tpl_id)
            return _err(_INTERNAL_ERROR, 500)

    async def semantic_memory_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT mb.bot_id, mb.username, mb.first_name,
                       COUNT(DISTINCT bum.user_id) AS conv_users,
                       COUNT(buf.id) AS fact_count
                FROM managed_bots mb
                LEFT JOIN bot_user_memory bum ON bum.bot_id = mb.bot_id
                LEFT JOIN bot_user_facts buf ON buf.bot_id = mb.bot_id
                WHERE mb.added_by = $1
                GROUP BY mb.bot_id, mb.username, mb.first_name
                ORDER BY fact_count DESC NULLS LAST
                LIMIT 30
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("semantic_memory_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def semantic_memory_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            owner = await pool.fetchval("SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id)
            if owner != uid:
                return _err("Это не ваш ресурс", 403)
            # Имя собеседника, а не его номер. Экран показывал «User 783215441»:
            # смысл семантической памяти в том, что это факты О ЧЕЛОВЕКЕ, а по
            # номеру человека не узнать ни владельцу, ни кому-либо ещё.
            facts = await pool.fetch(
                "SELECT f.user_id, f.fact_key, f.fact_value, f.confidence, f.updated_at, "
                "       bu.first_name, bu.last_name, bu.username "
                "FROM bot_user_facts f "
                "LEFT JOIN bot_users bu ON bu.bot_id = f.bot_id AND bu.user_id = f.user_id "
                "WHERE f.bot_id=$1 ORDER BY f.updated_at DESC LIMIT $2",
                bot_id, _list_limit(request, 100, 2000),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM bot_user_facts WHERE bot_id=$1", bot_id)
            # Было — голый массив; сотня фактов выдавалась за всю память бота.
            return _json_resp({"items": [
                {
                    "user_id": r["user_id"],
                    "fact_key": r["fact_key"],
                    "fact_value": r["fact_value"],
                    "confidence": r["confidence"],
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                    "name": " ".join(x for x in (r["first_name"], r["last_name"]) if x).strip(),
                    "username": r["username"] or "",
                }
                for r in facts
            ], "total": total})

        except Exception:
            log.exception("semantic_memory_bot uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def semantic_memory_forget(request: web.Request) -> web.Response:
        """Забыть всё, что бот запомнил об одном собеседнике.

        В продукте это умеет только Telegram-бот (semantic_memory_hub). А это
        не удобство, а обязанность: бот хранит имя, город, боли и цели живого
        человека, и владелец должен уметь это стереть, не уходя из приложения.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            user_id = int(request.match_info["user_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        try:
            owner = await pool.fetchval(
                "SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id)
            if owner != uid:
                return _err("Это не ваш ресурс", 403)
            from services import semantic_memory as _sm
            msgs, facts = await _sm.clear_user_memory(pool, bot_id, user_id)
            return _json_resp({"ok": True, "messages": msgs, "facts": facts})
        except Exception:
            log.exception("semantic_memory_forget uid=%d bot=%d user=%d",
                          uid, bot_id, user_id)
            return _err(_INTERNAL_ERROR, 500)

    async def audience_dna_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT ad.id, ad.bot_id, mb.username, mb.first_name,
                       ad.avg_engagement_rate, ad.churn_risk_pct,
                       ad.total_users_analyzed, ad.peak_hours, ad.peak_days,
                       ad.best_content_types, ad.top_topics, ad.computed_at
                FROM audience_dna ad
                LEFT JOIN managed_bots mb ON mb.bot_id = ad.bot_id
                WHERE ad.owner_id = $1
                ORDER BY ad.computed_at DESC
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("audience_dna_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def audience_dna_profile(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            from database import db as _db
            from services import audience_dna as dna_svc

            bot_row = await _db.get_bot(pool, bot_id, uid)
            if not bot_row:
                return _err("Не найдено", 404)

            dna = await dna_svc.get_dna(pool, bot_id)
            if not dna:
                return _json_resp({"computed": False})

            data = _dna_to_dict(dna)
            data["computed"] = True
            data["recommendations"] = dna_svc.generate_recommendations(dna)
            return _json_resp(data)
        except Exception:
            log.exception("audience_dna_profile uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def audience_dna_compute(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            from database import db as _db
            from services import audience_dna as dna_svc

            bot_row = await _db.get_bot(pool, bot_id, uid)
            if not bot_row:
                return _err("Не найдено", 404)

            dna = await dna_svc.compute_dna(pool, bot_id, uid)
            data = _dna_to_dict(dna)
            data["computed"] = True
            data["recommendations"] = dna_svc.generate_recommendations(dna)
            return _json_resp(data)
        except Exception:
            log.exception("audience_dna_compute uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def audience_dna_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            from database import db as _db
            from services import audience_dna as dna_svc

            bot_row = await _db.get_bot(pool, bot_id, uid)
            if not bot_row:
                return _err("Не найдено", 404)

            history = await dna_svc.get_dna_history(pool, bot_id, limit=10)
            return _json_resp([_dna_to_dict(snap) for snap in history])
        except Exception:
            log.exception("audience_dna_history uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/clone_adapt/history", clone_adapt_history)
    app.router.add_post("/api/miniapp/clone_adapt/submit", clone_adapt_submit)
    app.router.add_post("/api/miniapp/content_mesh", content_mesh_create)
    app.router.add_get("/api/miniapp/content_mesh/{mesh_id}/targets", content_mesh_targets_list)
    app.router.add_post("/api/miniapp/content_mesh/{mesh_id}/target", content_mesh_target_add)
    app.router.add_delete("/api/miniapp/content_mesh/target/{target_id}", content_mesh_target_delete)
    app.router.add_put("/api/miniapp/content_mesh/{mesh_id}/toggle", content_mesh_toggle)
    app.router.add_get("/api/miniapp/content_mesh/{mesh_id}", content_mesh_detail)
    app.router.add_put("/api/miniapp/content_mesh/{mesh_id}", content_mesh_update)
    app.router.add_delete("/api/miniapp/content_mesh/{mesh_id}", content_mesh_delete)
    app.router.add_put("/api/miniapp/content_mesh/target/{target_id}/toggle", content_mesh_target_toggle)
    app.router.add_get("/api/miniapp/narrative", narrative_campaigns_list)
    app.router.add_post("/api/miniapp/narrative", narrative_campaign_create)
    app.router.add_get("/api/miniapp/narrative/{campaign_id}", narrative_campaign_detail)
    app.router.add_post("/api/miniapp/narrative/{campaign_id}/pause", narrative_campaign_pause)
    app.router.add_post("/api/miniapp/narrative/{campaign_id}/resume", narrative_campaign_resume)
    app.router.add_post("/api/miniapp/spintax/generate", spintax_generate)
    app.router.add_post("/api/miniapp/spintax/expand", spintax_expand)
    app.router.add_get("/api/miniapp/self_promo", self_promo_list)
    app.router.add_get("/api/miniapp/self_promo/audience", self_promo_audience)
    app.router.add_post("/api/miniapp/self_promo/template", self_promo_create)
    app.router.add_delete("/api/miniapp/self_promo/{tpl_id}", self_promo_delete)
    app.router.add_put("/api/miniapp/self_promo/{tpl_id}/toggle", self_promo_toggle)
    app.router.add_post("/api/miniapp/self_promo/{tpl_id}/launch", self_promo_launch)
    app.router.add_get("/api/miniapp/semantic_memory", semantic_memory_overview)
    app.router.add_get("/api/miniapp/semantic_memory/{bot_id}", semantic_memory_bot)
    app.router.add_get("/api/miniapp/audience_dna", audience_dna_list)
    app.router.add_get("/api/miniapp/audience_dna/{bot_id}/profile", audience_dna_profile)
    app.router.add_post("/api/miniapp/audience_dna/{bot_id}/compute", audience_dna_compute)
    app.router.add_get("/api/miniapp/audience_dna/{bot_id}/history", audience_dna_history)
    app.router.add_delete("/api/miniapp/semantic_memory/{bot_id}/user/{user_id}",
                          semantic_memory_forget)
