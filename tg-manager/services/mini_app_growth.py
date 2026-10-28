"""Рост, SEO, эксперименты и аналитика — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 18 маршрутов четырёх блоков, которые
владелец видит как одну работу «понять, что даёт рост, и повторить».

* `/api/miniapp/growth*` — сводка роста, план и заявка на рост.
* `/api/miniapp/seo*` — разбор, применение подсказок к одному боту, ко всем
  сразу и к выбранному.
* `/api/miniapp/experiment*` — эксперименты: список, карточка, создание,
  смена статуса, объявление победителя, удаление.
* `/api/miniapp/analytics*` — сводка, дэшборд, реальное время, история.

Вместе с обработчиками уехали два замыкания, которых больше никто не зовёт:
`_EXP_STATUSES` (допустимые статусы эксперимента — один список на смену
статуса и на карточку, чтобы экран не предлагал статус, который ручка
отвергнет) и `_own_experiment(uid, exp_id)` (эксперимент принадлежит
владельцу; по `{exp_id}` из пути без него читался бы чужой — это держит
`tests/test_miniapp_path_param_scope_ratchet.py`).

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import re

from aiohttp import web

from services import operation_bus as _obus


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты роста, SEO, экспериментов и аналитики."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _safe_count, _safe_fetch,
        _safe_fetchrow, log,
    )
    from services.security import (
        check_sql_suspicious, validate_integer, validate_string,
    )

    _EXP_STATUSES = ("draft", "active", "paused", "completed")

    async def growth_overview(request: web.Request) -> web.Response:
        """Мотор роста: единый отчёт по инструментам продвижения за период.

        Агрегирует операции роста (накрутка/Growth Agent/self-promo) из
        operation_queue за N дней через чистый growth_center.summarize_growth,
        добавляет прирост участников по каналам (channel_member_history).
        Fail-open: сбой блока не рушит ответ."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.query.get("days", "30"))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 90))
        from services import growth_center
        rows = await _safe_fetch(pool,
            """SELECT op_type, status, done_items, total_items, created_at
               FROM operation_queue
               WHERE owner_id=$1 AND op_type = ANY($2::text[])
                 AND created_at > NOW() - ($3 || ' days')::interval
               ORDER BY created_at DESC""",
            uid, list(growth_center.GROWTH_OP_TYPES), str(days))
        summary = growth_center.summarize_growth([dict(r) for r in rows])
        # Прирост участников по каналам за период (лучшие 5 по d-приросту).
        channels: list[dict] = []
        try:
            crows = await _safe_fetch(pool,
                """SELECT mc.channel_id, mc.title, mc.username,
                          COALESCE(mc.members_count,0) AS current,
                          (SELECT members_count FROM channel_member_history h
                           WHERE h.owner_id=mc.owner_id AND h.channel_id=mc.channel_id
                             AND h.captured_on <= (CURRENT_DATE - ($2 || ' days')::interval)
                           ORDER BY h.captured_on DESC LIMIT 1) AS base
                   FROM managed_channels mc
                   WHERE mc.owner_id=$1 AND COALESCE(mc.members_count,0) > 0
                   ORDER BY mc.members_count DESC LIMIT 30""",
                uid, str(min(days, 30)))
            for r in crows:
                base = r["base"]
                delta = (int(r["current"]) - int(base)) if base is not None else None
                channels.append({"channel_id": r["channel_id"], "title": r["title"],
                                 "username": r["username"], "current": int(r["current"]),
                                 "delta": delta})
            channels.sort(key=lambda c: (c["delta"] is not None, c["delta"] or 0), reverse=True)
            channels = channels[:5]
        except Exception:
            log.debug("growth_overview: channel growth failed uid=%s", uid)
        return _json_resp({"days": days, "summary": summary, "channels": channels})

    async def growth_plan(request: web.Request) -> web.Response:
        """Цель «+N подписчиков к сроку» → раскладка по инструментам роста.

        Ёмкость флота считается как в campaign_planner.plan (аккаунты + суточные
        лимиты), затем чистый growth_center.recommend_plan раскладывает цель."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            goal = validate_integer(request.query.get("goal"), min_val=1, max_val=10_000_000) or 1000
            days = validate_integer(request.query.get("days"), min_val=1, max_val=365) or 14
        except (TypeError, ValueError):
            goal, days = 1000, 14
        accs = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
            "AND session_str IS NOT NULL", uid)
        acc_ids = [int(r["id"]) for r in (accs or [])]
        daily = 0
        try:
            from services import flood_engine
            for aid in acc_ids:
                d = await flood_engine.recommended_daily_limit(pool, aid)
                daily += int(d.get("limit", 0) or 0)
        except Exception:
            log.debug("growth_plan: capacity calc failed uid=%s", uid)
        from services import growth_center
        return _json_resp(growth_center.recommend_plan(
            int(goal), int(days), len(acc_ids), daily))

    async def growth_submit(request: web.Request) -> web.Response:
        """Growth Agent: постинг промо-текста в нишевых группах."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        niche = validate_string(body.get("niche"), max_len=200)
        geo = validate_string(body.get("geo"), max_len=60) or ""
        promo_text = validate_string(body.get("promo_text"), max_len=4096)
        try:
            acc_count = max(1, min(validate_integer(body.get("acc_count") or 3, min_val=1, max_val=10) or 3, 10))
        except (TypeError, ValueError):
            acc_count = 3
        if not niche:
            return _err("Укажите нишу", 400)
        if not promo_text:
            return _err("Укажите рекламный текст", 400)
        if check_sql_suspicious(promo_text):
            return _err("Недопустимые символы в тексте промо")
        from services import content_safety

        _v = await content_safety.enforce(pool, uid, niche, promo_text, surface="growth_agent")
        if _v.blocked:
            return _err("Контент заблокирован политикой платформы", 403)
        # Нужен хотя бы один активный аккаунт с сессией.
        has_acc = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL",
            uid)
        if not has_acc:
            return _err("Нет активных аккаунтов", 400)
        try:
            label = f"Growth Agent: {niche[:40]}"
            op_id = await _obus.submit(
                pool, uid, "niche_growth_post",
                {"niche": niche, "geo": geo, "promo_text": promo_text, "acc_count": acc_count},
                total_items=5, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("growth_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def seo_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            suggestions = await pool.fetch(
                """SELECT s.owner_id, s.chan_id, s.title, s.about, s.username, s.created_at,
                          c.title AS channel_title, c.username AS channel_username
                   FROM seo_ai_suggestions s
                   LEFT JOIN managed_channels c ON c.channel_id=s.chan_id
                   WHERE s.owner_id=$1 ORDER BY s.created_at DESC LIMIT 20""",
                uid,
            )
            keywords = await pool.fetch(
                "SELECT keyword, search_count FROM search_memory WHERE owner_id=$1 ORDER BY search_count DESC LIMIT 20",
                uid,
            )
            # Рекомендации по переоптимизации БОТОВ (авто-сгенерированы при падении
            # позиции; применяются в один клик через /api/miniapp/seo/apply_bot).
            bot_suggestions = await _safe_fetch(pool,
                """SELECT bs.bot_id, bs.name, bs.short_desc, bs.reason, bs.keyword,
                          bs.created_at, bs.applied_at, mb.username AS bot_username
                   FROM bot_seo_suggestions bs
                   LEFT JOIN managed_bots mb ON mb.bot_id=bs.bot_id
                   WHERE bs.owner_id=$1 ORDER BY bs.created_at DESC LIMIT 20""",
                uid) or []
            return _json_resp({
                "suggestions": [
                    {**dict(s), "created_at": s["created_at"].isoformat() if s["created_at"] else None}
                    for s in suggestions
                ],
                "bot_suggestions": [
                    {**dict(b),
                     "created_at": b["created_at"].isoformat() if b["created_at"] else None,
                     "applied_at": b["applied_at"].isoformat() if b["applied_at"] else None}
                    for b in bot_suggestions
                ],
                "keywords": [dict(k) for k in keywords],
            })
        except Exception:
            log.exception("seo_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def seo_apply(request: web.Request) -> web.Response:
        """Применить AI SEO-предложение к каналу в один клик (замыкает петлю
        анализ→рекомендация→ПРИМЕНЕНИЕ). Раньше предложения (title/about/username)
        только показывались — оптимизацию приходилось вбивать вручную.

        Переиспользует существующее: seo_ai_suggestions (хранимая рекомендация),
        managed_channels.acc_id → управляющий аккаунт, account_manager.edit_channel_*.
        body: {chan_id, fields?: ["title","about","username"]} — по умолчанию все.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        try:
            chan_id = int(body.get("chan_id"))
        except (TypeError, ValueError):
            return _err("chan_id обязателен", 400)
        fields = body.get("fields") or None

        # Единая реализация применения (та же, что в bulk-исполнителе) — без дублей.
        from services.seo_apply import apply_seo_to_channel
        try:
            res = await apply_seo_to_channel(pool, uid, chan_id, fields)
        except Exception:
            log.exception("seo_apply uid=%d chan=%d", uid, chan_id)
            return _err(_INTERNAL_ERROR, 500)

        if res.get("ok"):
            return _json_resp(res)
        return _err(res.get("error") or "Не удалось применить", 400)

    async def seo_apply_all(request: web.Request) -> web.Response:
        """Массовое применение SEO-предложений по ВСЕЙ сетке — в очередь (op_worker),
        т.к. правок много и инлайн упрётся в таймаут шлюза."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Каналы владельца, у которых есть хотя бы одно SEO-предложение и
        # управляющий аккаунт.
        rows = await _safe_fetch(pool,
            "SELECT DISTINCT mc.channel_id FROM managed_channels mc "
            "WHERE mc.owner_id=$1 AND mc.acc_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM seo_ai_suggestions s "
            "            WHERE s.owner_id=$1 AND s.chan_id=mc.channel_id)",
            uid)
        chan_ids = [int(r["channel_id"]) for r in (rows or [])]
        if not chan_ids:
            return _err("Нет каналов с SEO-предложениями для применения", 400)
        try:
            op_id = await _obus.submit(
                pool, uid, "bulk_seo_apply", {"channel_ids": chan_ids, "fields": ["about"]},
                total_items=len(chan_ids), label=f"SEO-описания · каналов: {len(chan_ids)}")
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        return _json_resp({"ok": True, "op_id": op_id, "count": len(chan_ids)})

    async def seo_apply_bot(request: web.Request) -> web.Response:
        """Применить рекомендацию по переоптимизации БОТА в один клик (замыкает
        петлю анализ→рекомендация→применение для стороны ботов; аналог
        /seo/apply для каналов). Рекомендация авто-генерируется при падении
        позиции (ranking_checker, opt-in). body: {bot_id, fields?}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        try:
            bot_id = int(body.get("bot_id"))
        except (TypeError, ValueError):
            return _err("bot_id обязателен", 400)
        fields = body.get("fields") or None
        from services import bot_reoptimizer
        try:
            res = await bot_reoptimizer.apply_bot_seo(pool, uid, bot_id, fields)
        except Exception:
            log.exception("seo_apply_bot uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if res.get("ok"):
            return _json_resp(res)
        return _err(res.get("error") or "Не удалось применить", 400)

    async def seo_analyze(request: web.Request) -> web.Response:
        """SEO-советник: находимость объекта в поиске Telegram + рекомендации."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        kw = body.get("keywords")
        if isinstance(kw, str):
            kw = [k for k in re.split(r"[,\n]+", kw) if k.strip()]
        from services import seo_advisor
        return _json_resp(seo_advisor.analyze(
            title=str(body.get("title") or "")[:200],
            username=str(body.get("username") or "")[:64],
            description=str(body.get("description") or "")[:1024],
            target_keywords=kw if isinstance(kw, list) else None))

    async def experiments_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT e.id, e.name, e.experiment_type, e.status, e.created_at,
                          b.username AS bot_username, b.first_name AS bot_name, e.bot_id,
                          (SELECT COUNT(*) FROM experiment_variants ev WHERE ev.experiment_id=e.id) AS variant_count
                   FROM experiments e
                   JOIN managed_bots b ON b.bot_id = e.bot_id
                   WHERE b.added_by=$1
                   ORDER BY e.created_at DESC LIMIT 50""",
                uid,
            )
            return _json_resp({"experiments": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception:
            log.exception("experiments_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def experiment_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор эксперимента", 400)
        try:
            exp = await pool.fetchrow(
                """SELECT e.id, e.name, e.experiment_type, e.status, e.created_at,
                          e.winner_variant_id, e.min_sample_size, e.bot_id,
                          b.username AS bot_username
                   FROM experiments e
                   JOIN managed_bots b ON b.bot_id=e.bot_id
                   WHERE e.id=$1 AND b.added_by=$2""",
                exp_id, uid,
            )
            if not exp:
                return _err("Не найдено", 404)
            variants = await pool.fetch(
                "SELECT id, name, content, weight, impressions, conversions FROM experiment_variants WHERE experiment_id=$1 ORDER BY id",
                exp_id,
            )
            # Экран показывал «CR: 12,3%» и молчал о том, можно ли этому
            # верить. Тот же z-тест, что и у A/B рассылок, — чтобы владелец не
            # остановил эксперимент на случайном отрыве.
            from services import ab_engine
            min_sample = int(exp["min_sample_size"] or 0) or 30
            verdict = ab_engine.pick_winner(
                [{"variant": v["id"], "sent": int(v["impressions"] or 0),
                  "converted": int(v["conversions"] or 0)} for v in variants],
                min_sample)
            return _json_resp({
                **dict(exp),
                "created_at": exp["created_at"].isoformat() if exp["created_at"] else None,
                "variants": [dict(v) for v in variants],
                "verdict": verdict,
                "min_sample": min_sample,
            })
        except Exception:
            log.exception("experiment_detail uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def experiment_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор эксперимента", 400)
        try:
            await pool.execute(
                "DELETE FROM experiments e USING managed_bots b WHERE e.id=$1 AND e.bot_id=b.bot_id AND b.added_by=$2",
                exp_id, uid,
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("experiment_delete uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def _own_experiment(uid: int, exp_id: int):
        """Эксперимент владельца или None. Скоуп — через владельца бота."""
        return await _safe_fetchrow(pool,
            """SELECT e.id, e.bot_id, e.status, e.experiment_type, e.name
                 FROM experiments e
                 JOIN managed_bots b ON b.bot_id = e.bot_id
                WHERE e.id=$1 AND b.added_by=$2""", exp_id, uid)

    async def experiment_set_status(request: web.Request) -> web.Response:
        """Запустить / поставить на паузу / завершить эксперимент.

        В боте это было, в мини-аппе — нет: статус показывался подписью, и
        изменить его из интерфейса было нечем.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор эксперимента", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        status = str(body.get("status") or "").strip().lower()
        if status not in _EXP_STATUSES:
            return _err("Неизвестный статус эксперимента", 400)
        exp = await _own_experiment(uid, exp_id)
        if not exp:
            return _err("Эксперимент не найден", 404)
        try:
            if status == "active":
                # Бот берёт ОДИН активный эксперимент на тип (get_active_experiment
                # с LIMIT 1): второй активный того же типа молча не исполнялся бы.
                busy = await _safe_fetchrow(pool,
                    """SELECT id, name FROM experiments
                        WHERE bot_id=$1 AND experiment_type=$2
                          AND status='active' AND id<>$3 LIMIT 1""",
                    exp["bot_id"], exp["experiment_type"], exp_id)
                if busy:
                    return _err(
                        "На этом боте уже идёт эксперимент «%s» того же типа. "
                        "Сначала поставьте его на паузу или завершите."
                        % (busy["name"] or busy["id"]), 409)
            from database import db as _db
            await _db.set_experiment_status(pool, exp_id, status, exp["bot_id"])
            return _json_resp({"ok": True, "status": status})
        except Exception:
            log.exception("experiment_set_status uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def experiment_set_winner(request: web.Request) -> web.Response:
        """Назначить победителя вручную — эксперимент при этом завершается."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор эксперимента", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        variant_id = validate_integer(body.get("variant_id"), min_val=1)
        if not variant_id:
            return _err("Не выбран вариант", 400)
        exp = await _own_experiment(uid, exp_id)
        if not exp:
            return _err("Эксперимент не найден", 404)
        try:
            # Вариант обязан принадлежать ИМЕННО этому эксперименту, иначе
            # победителем можно было бы назначить чужую строку.
            own = await _safe_fetchrow(pool,
                "SELECT id FROM experiment_variants WHERE id=$1 AND experiment_id=$2",
                variant_id, exp_id)
            if not own:
                return _err("Вариант не из этого эксперимента", 400)
            await pool.execute(
                "UPDATE experiments SET status='completed', winner_variant_id=$2, "
                "ended_at=NOW() WHERE id=$1 AND bot_id IN ("
                "SELECT bot_id FROM managed_bots WHERE added_by=$3)",
                exp_id, variant_id, uid)
            return _json_resp({"ok": True, "winner_variant_id": variant_id})
        except Exception:
            log.exception("experiment_set_winner uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def experiment_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        bot_id = data.get("bot_id")
        name = str(data.get("name", "")).strip()
        experiment_type = data.get("experiment_type", "start_message")
        variants = data.get("variants", [])
        if not bot_id or not name:
            return _err("bot_id и name обязательны", 400)
        if experiment_type not in ("start_message", "auto_reply", "funnel"):
            experiment_type = "start_message"
        if not variants or len(variants) < 2:
            return _err("Нужно минимум 2 варианта", 400)
        try:
            bot = await pool.fetchrow(
                "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", int(bot_id), uid
            )
            if not bot:
                return _err("Бот не найден", 404)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    exp_id = await conn.fetchval(
                        "INSERT INTO experiments (bot_id, name, experiment_type) VALUES ($1,$2,$3) RETURNING id",
                        int(bot_id), name, experiment_type,
                    )
                    for v in variants[:4]:
                        await conn.execute(
                            "INSERT INTO experiment_variants (experiment_id, name, content, weight) VALUES ($1,$2,$3,$4)",
                            exp_id,
                            str(v.get("name", "Вариант"))[:100],
                            str(v.get("content", ""))[:4000],
                            min(100, max(1, int(v.get("weight", 50)))),
                        )
            return _json_resp({"id": exp_id, "ok": True})
        except Exception:
            log.exception("experiment_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def analytics(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # New users per day (last 7 days) across all user's bots
        growth = await _safe_fetch(pool,
            """SELECT DATE(bu.first_seen) AS day, COUNT(*) AS new_users
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id=bu.bot_id
               WHERE mb.added_by=$1 AND bu.first_seen >= now() - INTERVAL '7 days'
               GROUP BY day ORDER BY day""", uid)
        # Top bots by subscribers
        top_bots = await _safe_fetch(pool,
            """SELECT mb.bot_id, mb.username, mb.first_name,
                      COUNT(DISTINCT bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
               WHERE mb.added_by=$1
               GROUP BY mb.bot_id, mb.username, mb.first_name
               ORDER BY active_subs DESC LIMIT 5""", uid)
        # Search keywords and last positions
        keywords = await _safe_fetch(pool,
            """SELECT tk.id, tk.keyword, mb.username AS bot_username,
                      (SELECT sr.position FROM search_rankings sr
                       WHERE sr.keyword_id=tk.id ORDER BY sr.checked_at DESC LIMIT 1) AS last_position
               FROM tracked_keywords tk
               JOIN managed_bots mb ON mb.bot_id=tk.bot_id
               WHERE tk.owner_id=$1 AND tk.is_active=true
               ORDER BY tk.created_at DESC LIMIT 10""", uid)
        # Потолки выдачи (5 ботов, 10 слов) на экране не признавались: подпись
        # «N ключей отслеживается» показывала 10 при сотне слов.
        kw_total = await _safe_count(pool,
            "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=true", uid)
        bots_total = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
        return _json_resp({
            "growth": growth,
            "top_bots": top_bots,
            "bots_total": int(bots_total or 0),
            "keywords": keywords,
            "keywords_total": int(kw_total or 0),
        })

    async def analytics_dashboard(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.analytics_dashboard import get_dashboard_stats
            stats = await get_dashboard_stats(pool, uid)
            return _json_resp(stats)
        except Exception:
            log.exception("analytics_dashboard uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def analytics_realtime(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.analytics_dashboard import get_realtime_metrics
            metrics = await get_realtime_metrics(pool, uid)
            return _json_resp(metrics)
        except Exception:
            log.exception("analytics_realtime uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def analytics_historical(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        metric = request.query.get("metric", "operations")
        try:
            days = max(1, min(365, int(request.query.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        try:
            from services.analytics_dashboard import get_historical_data
            data = await get_historical_data(pool, uid, metric, days)
            return _json_resp({"metric": metric, "days": days, "data": data})
        except Exception:
            log.exception("analytics_historical uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_post("/api/miniapp/seo/analyze", seo_analyze)
    app.router.add_get("/api/miniapp/analytics", analytics)
    app.router.add_get("/api/miniapp/experiments", experiments_list)
    app.router.add_post("/api/miniapp/experiment", experiment_create)
    app.router.add_get("/api/miniapp/experiment/{exp_id}", experiment_detail)
    app.router.add_delete("/api/miniapp/experiment/{exp_id}", experiment_delete)
    app.router.add_post("/api/miniapp/experiment/{exp_id}/status", experiment_set_status)
    app.router.add_post("/api/miniapp/experiment/{exp_id}/winner", experiment_set_winner)
    app.router.add_post("/api/miniapp/growth", growth_submit)
    app.router.add_get("/api/miniapp/growth/overview", growth_overview)
    app.router.add_get("/api/miniapp/growth/plan", growth_plan)
    app.router.add_get("/api/miniapp/seo", seo_overview)
    app.router.add_post("/api/miniapp/seo/apply", seo_apply)
    app.router.add_post("/api/miniapp/seo/apply_all", seo_apply_all)
    app.router.add_post("/api/miniapp/seo/apply_bot", seo_apply_bot)
    app.router.add_get("/api/miniapp/analytics/dashboard", analytics_dashboard)
    app.router.add_get("/api/miniapp/analytics/realtime", analytics_realtime)
    app.router.add_get("/api/miniapp/analytics/historical", analytics_historical)
