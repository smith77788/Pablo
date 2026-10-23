"""Рассылки, CRM и кампании — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 16 маршрутов четырёх смежных блоков,
которые владелец видит как одну работу «написать людям и довести до сделки».

* `/api/miniapp/broadcast*` — запуск рассылки, список получателей, повтор по
  неудавшимся, A/B-тест и аналитика по рассылке.
* `/api/miniapp/crm*` — сделки (список, создание, перенос стадии, удаление) и
  контакты (список, создание, удаление).
* `/api/miniapp/campaign/*` — план кампании, запуск и состояние.
* `/api/miniapp/template*` и `/api/miniapp/asset_template*` — шаблоны
  постов: список, создание, карточка, удаление. Создание зарегистрировано
  по двум путям (`/template` и `/asset_template`) — так было в монолите, и
  экран зовёт оба.
* `/api/miniapp/broadcasts` и `/api/miniapp/broadcast(s)/schedule` —
  история рассылок и отложенная рассылка. Они оставались в монолите, пока
  остальной блок рассылок уже жил здесь: один блок в двух файлах.

Рассылка — массовая операция, поэтому она НЕ пишет в `operation_queue`
напрямую, а уходит через `operation_bus.submit` (`_obus`): это стережёт
`tests/test_operation_bus_ratchet.py`. Честный счётчик обещанного и
отправленного держит `tests/test_op_progress_is_reported.py`.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import json as _json

from aiohttp import web

from services import operation_bus as _obus


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты рассылок, CRM и кампаний."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _safe_count, _safe_fetch,
        _safe_fetchrow, _text, log,
    )
    from services.security import (
        check_sql_suspicious, escape_html, validate_integer, validate_string,
    )


    async def create_broadcast(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        bot_id = validate_integer(body.get("bot_id"), min_val=1)
        text = validate_string(body.get("text"), max_len=4096)
        if not bot_id or not text:
            return _err("Нужны идентификатор бота и текст")
        # SQL injection defense on text
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте сообщения")
        if len(text) > 4096:
            return _err("Сообщение длиннее 4096 символов")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Неверный идентификатор бота")
        silent = bool(body.get("silent"))
        # Сегмент аудитории: all | active_7d | active_30d
        segment = validate_string(body.get("segment") or "all", max_len=20) or "all"
        _seg_sql = {
            "active_7d": " AND last_seen >= now() - interval '7 days'",
            "active_30d": " AND last_seen >= now() - interval '30 days'",
            # Спящие: ДНК аудитории советует реактивационную рассылку, а выбрать
            # эту аудиторию было нечем. Порог 14 дней — тот же, по которому
            # audience_dna считает churn_risk_pct.
            "inactive_14d": " AND (last_seen IS NULL OR last_seen < now() - interval '14 days')",
        }.get(segment, "")
        # Инлайн-кнопки (необязательно): [{text, url}] — валидируем и ограничиваем.
        buttons = []
        for b in (body.get("buttons") or [])[:10]:
            try:
                bt = validate_string(b.get("text"), max_len=64) or ""
                bu = validate_string(b.get("url"), max_len=2048) or ""
            except Exception:
                continue
            if bt and bu.lower().startswith(("http://", "https://")):
                buttons.append({"text": escape_html(bt), "url": bu})
        bot_row = await _safe_fetchrow(pool,
            "SELECT bot_id, token, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id_int, uid)
        if not bot_row:
            return _err("Бот не найден", 404)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true "
            "AND is_blocked=false AND suspect=false" + _seg_sql, bot_id_int)
        # Отложенная отправка: schedule_minutes минут от текущего момента.
        try:
            schedule_minutes = max(0, min(int(body.get("schedule_minutes") or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            schedule_minutes = 0
        try:
            bot_label = bot_row.get("username") or bot_id_int
            label = f"Рассылка боту @{bot_label}: {text[:40]}…" if len(text) > 40 else f"Рассылка: {text[:60]}"
            _op_params = {"bot_id": bot_id_int, "text": text}
            if buttons:
                _op_params["buttons"] = buttons
            if silent:
                _op_params["silent"] = True
            if _seg_sql:
                _op_params["segment"] = segment
            # Autopost v2: рекуррентная рассылка (0 = разовая). Движок op_worker
            # сам переочередит следующий запуск через repeat_interval_min минут.
            try:
                _rmin = max(0, min(int(body.get("repeat_interval_min") or 0), 60 * 24 * 30))
            except (TypeError, ValueError):
                _rmin = 0
            if _rmin > 0:
                _op_params["repeat_interval_min"] = _rmin
                _rc = body.get("repeat_count")
                if _rc is not None:
                    try:
                        _op_params["repeat_count"] = max(1, min(int(_rc), 1000))
                    except (TypeError, ValueError):
                        pass

            broadcast_id = None
            if schedule_minutes <= 0:
                # Немедленная отправка: создаём запись рассылки сразу (прогресс/resume).
                try:
                    row = await pool.fetchrow(
                        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, buttons, silent) "
                        "VALUES($1,$2,$3,'pending',$4,$5::jsonb,$6) RETURNING id",
                        bot_id_int, text, total, uid, _json.dumps(buttons) if buttons else None, silent)
                except Exception:
                    try:
                        row = await pool.fetchrow(
                            "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, buttons) "
                            "VALUES($1,$2,$3,'pending',$4,$5::jsonb) RETURNING id",
                            bot_id_int, text, total, uid, _json.dumps(buttons) if buttons else None)
                    except Exception:
                        row = await pool.fetchrow(
                            "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) VALUES($1,$2,$3,'pending',$4) RETURNING id",
                            bot_id_int, text, total, uid)
                broadcast_id = row["id"]
                _op_params["broadcast_id"] = broadcast_id
                try:
                    op_id = await _obus.submit(
                        pool, uid, "run_broadcast", _op_params,
                        total_items=total, label=label)
                except Exception:
                    # Постановка имеет право отказать (тариф, предохранитель Ban
                    # Weather), и тогда созданная выше запись рассылки обязана
                    # перестать быть «ожидающей»: иначе старт процесса поднимет
                    # её и отправит всей аудитории мимо отказа.
                    from services import broadcaster as _bc
                    await _bc.abandon_unstarted(pool, broadcast_id)
                    raise
            else:
                # Отложенная: НЕ создаём запись рассылки заранее (иначе resume
                # отправит её сразу при рестарте). Воркер создаст её, когда
                # наступит scheduled_for. Планировщик воркера уважает scheduled_for.
                label = f"⏰ {label}"
                from datetime import datetime as _dtm, timedelta as _tdl, timezone as _tzn
                op_id = await _obus.submit(
                    pool, uid, "run_broadcast", _op_params,
                    total_items=total, label=label,
                    scheduled_for=_dtm.now(_tzn.utc) + _tdl(minutes=schedule_minutes))
            return _json_resp({"ok": True, "broadcast_id": broadcast_id, "op_id": op_id,
                               "total_users": total, "scheduled_minutes": schedule_minutes})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("create_broadcast bot=%d uid=%d", bot_id_int, uid)
            return _err("Не удалось создать рассылку", 500)

    async def broadcast_recipients(request: web.Request) -> web.Response:
        """Точное число получателей для бота+сегмента — честный предпросмотр.

        Раньше UI показывал ПОЛНОЕ число подписчиков даже при выборе сегмента
        «Активным 7д/30д» → предпросмотр врал (бэк слал по сегменту, а число было
        от всей аудитории). Тот же _seg_sql, что и в create_broadcast/исполнителе.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        bot_id = validate_integer(request.rel_url.query.get("bot_id"), min_val=1)
        if not bot_id:
            return _err("Нужен идентификатор бота", 400)
        segment = validate_string(request.rel_url.query.get("segment") or "all", max_len=20) or "all"
        _seg_sql = {
            "active_7d": " AND last_seen >= now() - interval '7 days'",
            "active_30d": " AND last_seen >= now() - interval '30 days'",
            # Спящие: ДНК аудитории советует реактивационную рассылку, а выбрать
            # эту аудиторию было нечем. Порог 14 дней — тот же, по которому
            # audience_dna считает churn_risk_pct.
            "inactive_14d": " AND (last_seen IS NULL OR last_seen < now() - interval '14 days')",
        }.get(segment, "")
        # Владение ботом обязательно (иначе можно посчитать чужую аудиторию).
        owns = await _safe_fetchrow(pool,
            "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            int(bot_id), uid)
        if not owns:
            return _err("Бот не найден", 404)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true "
            "AND is_blocked=false AND suspect=false" + _seg_sql,
            int(bot_id))
        return _json_resp({"count": int(total or 0), "segment": segment})

    async def broadcast_resend(request: web.Request) -> web.Response:
        """Повторная отправка рассылки только НЕдоставленным получателям."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bc_id = int(request.match_info["bc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор рассылки", 400)
        # Единая реализация (общая с ботом) — не дублируем логику недоставленных.
        from services import broadcaster
        res = await broadcaster.resend_undelivered(pool, uid, bc_id)
        if not res.get("ok"):
            return _err(res.get("error", "Ошибка"), res.get("code", 400))
        return _json_resp(res)

    async def broadcast_ab_test(request: web.Request) -> web.Response:
        """A/B тестирование рассылок: POST /api/miniapp/broadcast/ab_test"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        bot_id = validate_integer(body.get("bot_id"), min_val=1)
        variants = body.get("variants")
        if not bot_id or not variants:
            return _err("Нужны идентификатор бота и варианты")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Неверный идентификатор бота")
        if not isinstance(variants, list) or len(variants) < 2:
            return _err("Вариантов должно быть не меньше двух, списком")
        for v in variants:
            if not isinstance(v, dict) or not v.get("text"):
                return _err("У каждого варианта должен быть текст")
        try:
            from services.broadcaster import ab_test_broadcast
            result = await ab_test_broadcast(pool, uid, bot_id_int, variants)
            if result.get("ok"):
                return _json_resp(result)
            return _err(result.get("error") or "Не удалось поставить рассылку", 400)
        except Exception:
            log.exception("broadcast_ab_test uid=%d bot=%d", uid, bot_id_int)
            return _err("Не удалось создать A/B-рассылку", 500)

    async def broadcast_analytics(request: web.Request) -> web.Response:
        """Аналитика рассылки: GET /api/miniapp/broadcast/{id}/analytics"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bc_id = int(request.match_info["bc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор рассылки", 400)
        try:
            from services.broadcaster import get_broadcast_analytics
            result = await get_broadcast_analytics(pool, uid, bc_id)
            if result.get("ok"):
                return _json_resp(result)
            return _err(result.get("error", "Not found"), 404)
        except Exception:
            log.exception("broadcast_analytics uid=%d bc=%d", uid, bc_id)
            return _err("Не удалось получить аналитику", 500)

    async def campaign_plan(request: web.Request) -> web.Response:
        """Планировщик кампании: цель (+N участников к сроку) → ёмкостная раскладка."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        goal = validate_integer(body.get("goal_count"), min_val=1, max_val=1000000)
        if not goal:
            return _err("Укажите цель (+N участников)", 400)
        deadline = validate_integer(body.get("deadline_days"), min_val=1, max_val=365) or 7
        method = str(body.get("method") or "direct").strip().lower()
        if method not in ("direct", "admin", "link"):
            method = "direct"
        try:
            from services import campaign_planner
            rep = await campaign_planner.plan(pool, uid, goal, deadline, method)
            # Цель становится общим состоянием организма (мозг о ней знает).
            try:
                from services.organism import spine
                await spine.state_set(pool, uid, "goal", {
                    "goal": goal, "deadline_days": deadline, "method": method,
                    "label": f"+{goal} за {deadline} дн."})
            except Exception:
                pass
            return _json_resp(rep)
        except Exception:
            log.exception("campaign_plan uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def crm_deals(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        stage = request.rel_url.query.get("stage")
        try:
            if stage:
                rows = await pool.fetch(
                    "SELECT id, title, contact, stage, value, notes, created_at, updated_at "
                    "FROM crm_deals WHERE owner_id=$1 AND stage=$2 ORDER BY updated_at DESC LIMIT 50",
                    uid, stage,
                )
            else:
                rows = await pool.fetch(
                    "SELECT id, title, contact, stage, value, notes, created_at, updated_at "
                    "FROM crm_deals WHERE owner_id=$1 ORDER BY updated_at DESC LIMIT 50",
                    uid,
                )
        except Exception:
            log.exception("crm_deals uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        # Also get pipeline summary
        try:
            summary_rows = await pool.fetch(
                "SELECT stage, COUNT(*) AS cnt, COALESCE(SUM(value),0) AS total "
                "FROM crm_deals WHERE owner_id=$1 GROUP BY stage",
                uid,
            )
        except Exception:
            summary_rows = []
        summary = {r["stage"]: {"count": int(r["cnt"]), "total": float(r["total"])} for r in summary_rows}
        return _json_resp({
            "deals": [
                {
                    "id": r["id"], "title": r["title"] or "",
                    "contact": r["contact"] or "", "stage": r["stage"] or "lead",
                    "value": float(r["value"]) if r["value"] else 0,
                    "notes": (r["notes"] or "")[:100],
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ],
            "summary": summary,
        })

    async def create_crm_deal(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            title = str(body.get("title", "")).strip()
            contact = str(body.get("contact", "")).strip()
            stage = str(body.get("stage", "lead")).strip()
            value = float(body.get("value", 0) or 0)
            notes = str(body.get("notes", "")).strip()
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not title:
            return _err("title обязателен", 400)
        valid_stages = ("lead", "contact", "proposal", "negotiation", "won", "lost")
        if stage not in valid_stages:
            stage = "lead"
        try:
            deal_id = await pool.fetchval(
                """INSERT INTO crm_deals(owner_id, title, contact, stage, value, notes)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                uid, title, contact or None, stage, value, notes or None,
            )
        except Exception:
            log.exception("create_crm_deal uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "deal_id": deal_id})

    async def update_crm_deal_stage(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            deal_id = int(request.match_info["deal_id"])
            body = await request.json()
            stage = str(body.get("stage", "")).strip()
        except Exception:
            return _err("Неверный запрос", 400)
        valid_stages = ("lead", "contact", "proposal", "negotiation", "won", "lost")
        if stage not in valid_stages:
            return _err("Неверная стадия", 400)
        try:
            res = await pool.execute(
                "UPDATE crm_deals SET stage=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
                stage, deal_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("update_crm_deal_stage uid=%d deal=%d", uid, deal_id)
            return _err(_INTERNAL_ERROR, 500)

    async def delete_crm_deal(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            deal_id = int(request.match_info["deal_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор сделки", 400)
        try:
            res = await pool.execute(
                "DELETE FROM crm_deals WHERE id=$1 AND owner_id=$2", deal_id, uid
            )
            if res == "DELETE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_crm_deal uid=%d deal=%d", uid, deal_id)
            return _err(_INTERNAL_ERROR, 500)

    async def crm_contacts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        search = (request.query.get("q") or "").strip()
        tag = (request.query.get("tag") or "").strip()
        try:
            offset = max(0, int(request.query.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        if search:
            rows = await _safe_fetch(pool,
                """SELECT id, tg_user_id, username, first_name, last_name, phone,
                          tags, notes, source, created_at
                   FROM crm_contacts WHERE owner_id=$1
                     AND (first_name ILIKE $2 OR last_name ILIKE $2 OR username ILIKE $2 OR phone ILIKE $2)
                   ORDER BY created_at DESC, id LIMIT 50 OFFSET $3""",
                uid, f"%{search}%", offset)
        elif tag:
            rows = await _safe_fetch(pool,
                """SELECT id, tg_user_id, username, first_name, last_name, phone,
                          tags, notes, source, created_at
                   FROM crm_contacts WHERE owner_id=$1 AND $2=ANY(tags)
                   ORDER BY created_at DESC, id LIMIT 50 OFFSET $3""",
                uid, tag, offset)
        else:
            rows = await _safe_fetch(pool,
                """SELECT id, tg_user_id, username, first_name, last_name, phone,
                          tags, notes, source, created_at
                   FROM crm_contacts WHERE owner_id=$1
                   ORDER BY created_at DESC, id LIMIT 50 OFFSET $2""",
                uid, offset)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1", uid)
        # Unique tags across all contacts
        tags_row = await _safe_fetch(pool,
            "SELECT DISTINCT unnest(tags) AS tag FROM crm_contacts WHERE owner_id=$1 ORDER BY tag", uid)
        all_tags = [r["tag"] for r in tags_row]
        return _json_resp({"contacts": rows, "total": total, "offset": offset, "all_tags": all_tags})

    async def crm_contact_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        first_name = str(data.get("first_name", "")).strip()
        last_name = str(data.get("last_name", "")).strip() or None
        username = str(data.get("username", "")).strip().lstrip("@") or None
        phone = str(data.get("phone", "")).strip() or None
        tg_user_id_raw = data.get("tg_user_id")
        tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
        notes = str(data.get("notes", "")).strip() or None
        if not first_name and not username and not phone:
            return _err("Укажите имя, @username или номер телефона", 400)
        tg_user_id = int(tg_user_id_raw) if tg_user_id_raw else None
        if not tg_user_id:
            import random as _rand
            tg_user_id = _rand.randint(2_000_000_000, 9_000_000_000)
        try:
            cid = await pool.fetchval(
                """INSERT INTO crm_contacts
                   (owner_id, tg_user_id, first_name, last_name, username, phone, tags, notes, source)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'manual')
                   ON CONFLICT (owner_id, tg_user_id) DO UPDATE
                     SET first_name=EXCLUDED.first_name,
                         last_name=EXCLUDED.last_name,
                         username=EXCLUDED.username,
                         phone=EXCLUDED.phone,
                         tags=EXCLUDED.tags,
                         notes=EXCLUDED.notes,
                         updated_at=now()
                   RETURNING id""",
                uid, tg_user_id, first_name or None, last_name, username, phone,
                tags, notes,
            )
            return _json_resp({"id": cid, "ok": True})
        except Exception:
            log.exception("crm_contact_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def crm_contact_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            contact_id = int(request.match_info["contact_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор контакта", 400)
        try:
            await pool.execute(
                "DELETE FROM crm_contacts WHERE id=$1 AND owner_id=$2", contact_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("crm_contact_delete uid=%d cid=%d", uid, contact_id)
            return _err(_INTERNAL_ERROR, 500)

    async def delete_template(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шаблона", 400)
        try:
            await pool.execute(
                "DELETE FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_template")
            return _err("Не удалось удалить", 500)

    async def campaign_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            spec = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        from services import campaign_hub
        try:
            res = await campaign_hub.launch_campaign(pool, uid, spec)
        except campaign_hub.CampaignSpecError as exc:
            return _err(str(exc), 400)
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("campaign_launch uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not res.get("children"):
            why = "; ".join(res.get("errors") or []) or "нет целей для запуска"
            return _err(f"Кампания не запущена: {why}", 400)
        return _json_resp({"ok": True, **res})

    async def campaign_status_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор кампании", 400)
        from services import campaign_hub
        try:
            st = await campaign_hub.campaign_status(pool, uid, cid)
        except Exception:
            log.exception("campaign_status uid=%s cid=%s", uid, cid)
            return _err(_INTERNAL_ERROR, 500)
        if st is None:
            return _err("Кампания не найдена", 404)
        return _json_resp(st)

    async def broadcasts_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # История была обрезана тридцатью без признака обрезки: тридцать первая
        # рассылка просто не существовала для владельца, и понять это было
        # неоткуда. Плюс срез по статусу — неудачные рассылки ищут именно так.
        try:
            limit = min(validate_integer(request.query.get("limit", "30"),
                                         min_val=1, max_val=200) or 30, 200)
        except (ValueError, TypeError):
            limit = 30
        try:
            offset = max(validate_integer(request.query.get("offset", "0"),
                                          min_val=0, max_val=100000) or 0, 0)
        except (ValueError, TypeError):
            offset = 0
        status = (request.query.get("status") or "").strip().lower()

        counts: dict = {}
        try:
            agg = await pool.fetch(
                "SELECT status, COUNT(*) AS n, "
                "       COALESCE(SUM(sent_count), 0)   AS sent, "
                "       COALESCE(SUM(failed_count), 0) AS failed "
                "FROM broadcasts WHERE created_by=$1 GROUP BY status", uid)
            sent_all = failed_all = 0
            for r in agg:
                counts[(r["status"] or "").lower() or "unknown"] = int(r["n"] or 0)
                sent_all += int(r["sent"] or 0)
                failed_all += int(r["failed"] or 0)
            # Доставляемость по ВСЕЙ истории: экран расписания считал её по
            # странице в тридцать строк и показывал как общий процент.
            totals = {"sent": sent_all, "failed": failed_all}
        except Exception as exc:
            log.warning("broadcasts uid=%d: агрегат недоступен: %s", uid, exc)
            totals = {}
        total = sum(counts.values()) if counts else await _safe_count(
            pool, "SELECT COUNT(*) FROM broadcasts WHERE created_by=$1", uid)
        # Неизвестный статус не отбрасываем молча: если его нет среди
        # существующих, список по нему пуст, и это честный ответ.
        if status and status not in counts:
            status = ""

        cols = """b.id, b.bot_id, mb.username AS bot_username,
                  b.message_text, b.status, b.sent_count, b.failed_count,
                  b.total_users, b.created_at"""
        if status:
            rows = await _safe_fetch(pool,
                f"""SELECT {cols} FROM broadcasts b
                    JOIN managed_bots mb ON mb.bot_id=b.bot_id
                    WHERE b.created_by=$1 AND lower(b.status)=$2
                    ORDER BY b.created_at DESC, b.id LIMIT $3 OFFSET $4""",
                uid, status, limit, offset)
            shown_total = counts.get(status, len(rows) + offset)
        else:
            rows = await _safe_fetch(pool,
                f"""SELECT {cols} FROM broadcasts b
                    JOIN managed_bots mb ON mb.bot_id=b.bot_id
                    WHERE b.created_by=$1
                    ORDER BY b.created_at DESC, b.id LIMIT $2 OFFSET $3""",
                uid, limit, offset)
            shown_total = total
        return _json_resp({
            "broadcasts": rows,
            "total": total,
            "counts": counts,
            "totals": totals,
            "status": status,
            "page": {"offset": offset, "limit": limit,
                     "has_more": offset + len(rows) < shown_total},
        })

    async def broadcast_schedule(request: web.Request) -> web.Response:
        """Рассылка с расписанием: POST /api/miniapp/broadcast/schedule"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        bot_id = validate_integer(body.get("bot_id"), min_val=1)
        # Фронт (submitScheduledBroadcast) шлёт message_text/scheduled_at — принимаем
        # оба контракта (иначе кнопка «Запланировать» отдавала бы 400/404).
        text = validate_string(body.get("text") or body.get("message_text"), max_len=4096)
        if not bot_id or not text:
            return _err("Нужны идентификатор бота и текст")
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте сообщения")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Неверный идентификатор бота")
        schedule = {
            "schedule_minutes": body.get("schedule_minutes"),
            "scheduled_for": body.get("scheduled_for") or body.get("scheduled_at"),
            "segment": body.get("segment"),
            "buttons": body.get("buttons"),
        }
        try:
            from services.broadcaster import mass_broadcast_with_scheduling
            result = await mass_broadcast_with_scheduling(pool, uid, bot_id_int, text, schedule)
            if result.get("ok"):
                return _json_resp(result)
            return _err(result.get("error") or "Не удалось поставить рассылку", 400)
        except Exception:
            log.exception("broadcast_schedule uid=%d bot=%d", uid, bot_id_int)
            return _err("Не удалось создать отложенную рассылку", 500)

    async def templates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, name, asset_type, template, created_at
               FROM asset_templates WHERE owner_id=$1 AND asset_type='post'
               ORDER BY created_at DESC LIMIT 30""", uid)
        for r in rows:
            if isinstance(r.get("template"), str):
                try:
                    r["template"] = _json.loads(r["template"])
                except Exception:
                    r["template"] = {}
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM asset_templates WHERE owner_id=$1 AND asset_type='post'", uid)
        return _json_resp({"templates": rows, "total": int(total or 0)})

    async def create_template(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        name = _text(body.get("name"))
        text = _text(body.get("text"))
        if not name:
            return _err("Нужно название")
        if not text:
            return _err("Нужен текст")
        if len(text) > 4096:
            return _err("Текст длиннее 4096 символов")
        try:
            row = await pool.fetchrow(
                "INSERT INTO asset_templates(owner_id, asset_type, name, template) VALUES($1,'post',$2,$3::jsonb) RETURNING id",
                uid, name, _json.dumps({"text": text}))
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("create_template uid=%d", uid)
            return _err("Не удалось создать шаблон", 500)

    async def asset_template_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шаблона", 400)
        try:
            tpl = await pool.fetchrow(
                "SELECT * FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
            )
            if not tpl:
                return _err("Не найдено", 404)
            return _json_resp(dict(tpl))
        except Exception:
            log.exception("asset_template_detail uid=%d tpl=%d", uid, tpl_id)
            return _err(_INTERNAL_ERROR, 500)

    async def asset_template_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шаблона", 400)
        try:
            result = await pool.execute(
                "DELETE FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
            )
            if result == "DELETE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("asset_template_delete uid=%d tpl=%d", uid, tpl_id)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_post("/api/miniapp/campaign/plan", campaign_plan)
    app.router.add_post("/api/miniapp/broadcast", create_broadcast)
    app.router.add_get("/api/miniapp/broadcast/recipients", broadcast_recipients)
    app.router.add_post("/api/miniapp/broadcast/{bc_id}/resend", broadcast_resend)
    app.router.add_post("/api/miniapp/broadcast/ab_test", broadcast_ab_test)
    app.router.add_get("/api/miniapp/broadcast/{bc_id}/analytics", broadcast_analytics)
    app.router.add_get("/api/miniapp/crm/contacts", crm_contacts)
    app.router.add_post("/api/miniapp/crm/contact", crm_contact_create)
    app.router.add_delete("/api/miniapp/crm/contact/{contact_id}", crm_contact_delete)
    app.router.add_delete("/api/miniapp/template/{tpl_id}", delete_template)
    app.router.add_get("/api/miniapp/crm/deals", crm_deals)
    app.router.add_post("/api/miniapp/crm/deal", create_crm_deal)
    app.router.add_put("/api/miniapp/crm/deal/{deal_id}/stage", update_crm_deal_stage)
    app.router.add_delete("/api/miniapp/crm/deal/{deal_id}", delete_crm_deal)
    app.router.add_post("/api/miniapp/campaign/launch", campaign_launch)
    app.router.add_get("/api/miniapp/campaign/{campaign_id}", campaign_status_get)
    app.router.add_get("/api/miniapp/broadcasts", broadcasts_list)
    app.router.add_post("/api/miniapp/broadcast/schedule", broadcast_schedule)
    app.router.add_post("/api/miniapp/broadcasts/schedule", broadcast_schedule)
    app.router.add_get("/api/miniapp/templates", templates)
    app.router.add_post("/api/miniapp/template", create_template)
    app.router.add_post("/api/miniapp/asset_template", create_template)
    app.router.add_get("/api/miniapp/asset_template/{tpl_id}", asset_template_detail)
    app.router.add_delete("/api/miniapp/asset_template/{tpl_id}", asset_template_delete)
