"""Боты владельца — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 36 маршрутов `/api/miniapp/bot/*` и
`/api/miniapp/bots*` и 35 функций лежали в файле на 24 тысячи строк вперемешку
с контактами, флотом, облаком и A/B. Это второй по размеру связный блок
приложения после хранилища контактов: подключение бота по токену, автоответы,
воронки, расписание, рассылка подписчикам бота, скан найденных ботов,
диалоги и статистика.

Здесь — только разбор запроса, скоуп владельца (`_user_can_use_bot`), русские
ошибки и вызов движка; логика живёт в `services/bot_*`. Поведение при переносе
не менялось: тела обработчиков и строки регистрации маршрутов перенесены
дословно.

`bots_bulk_edit` оставлен в монолите: это двухстрочная обёртка над общим
замыканием `_bulk_edit_op`, которым пользуются ещё массовая правка каналов и
профилей аккаунтов — переносить вместе с ним пришлось бы и их.
"""
from __future__ import annotations

import json as _json

import asyncpg
from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты ботов на приложении мини-аппа."""
    from services import operation_bus as _obus
    from services.mini_app_api import (
        _BOTS_VISIBLE_SQL, _INTERNAL_ERROR, _cached_user, _err, _get_uid,
        _json_resp, _safe_count, _safe_fetch, _safe_fetchrow, _str_list, _text,
        _user_can_use_bot, log, schedule_repeat_minutes,
    )
    from services.security import (
        escape_html, resolve_url_is_public, validate_bot_token,
        validate_integer, validate_start_param, validate_string,
    )

    @_cached_user()
    async def bots(request: web.Request) -> web.Response:
        """Боты пользователя: страница по limit/offset и честное общее число.

        Эндпоинт не умел offset: «загрузить ещё» перезапрашивало срез побольше
        (500 → 1000 → …) с потолком в 5000, и на этом потолке экран писал
        «больше сервер за раз не отдаёт, сузьте поиск». Совет было невозможно
        выполнить: поиск шёл по тем же загруженным строкам, и боты за потолком
        оставались недостижимы в принципе. Теперь страница идёт по offset, и
        потолка нет.

        Серверный поиск и срез по состоянию — отдельная задача: на клиенте
        `BOTS` служит ещё и источником для выбора бота и для рассылки по сети,
        и сузить этот набор молча нельзя.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.rel_url.query
        try:
            limit = min(validate_integer(q.get("limit", "500"), min_val=1, max_val=5000) or 500, 5000)
        except (ValueError, TypeError):
            limit = 500
        try:
            offset = max(validate_integer(q.get("offset", "0"), min_val=0) or 0, 0)
        except (ValueError, TypeError):
            offset = 0
        # Колонки здоровья добавляет inline-миграция на старте. Если она не
        # прошла, список ботов не должен исчезнуть целиком: _safe_fetch вернул
        # бы пустоту, неотличимую от «ботов нет». Поэтому — запасной запрос
        # без них (см. ту же защиту в обзоре прогрева).
        _BOT_HEALTH_COLS = "mb.last_error, mb.fail_streak, mb.last_ok_at,"
        _BOTS_SQL = """SELECT DISTINCT mb.bot_id, mb.username, mb.first_name, mb.is_active,
                      {health}
                      COUNT(DISTINCT bu.user_id) FILTER (WHERE bu.is_active=true) AS subscriber_count,
                      COUNT(DISTINCT bu.user_id) AS total_users
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
               WHERE ({visible})
               GROUP BY mb.bot_id, mb.username, mb.first_name, mb.is_active{group}
               ORDER BY subscriber_count DESC, mb.bot_id LIMIT $2 OFFSET $3"""
        try:
            rows = await pool.fetch(
                _BOTS_SQL.format(health=_BOT_HEALTH_COLS, visible=_BOTS_VISIBLE_SQL,
                                 group=",\n mb.last_error, mb.fail_streak, mb.last_ok_at"),
                uid, limit, offset)
        except Exception:
            log.warning("bots uid=%d: нет колонок здоровья, показываем список без них", uid)
            rows = await _safe_fetch(
                pool, _BOTS_SQL.format(health="", visible=_BOTS_VISIBLE_SQL, group=""),
                uid, limit, offset)
        # Честное состояние каждого бота. Раньше бот с отозванным токеном
        # выглядел здесь ровно как рабочий: подписчики ему писали, ответа не
        # было, и об этом знал только серверный лог.
        try:
            from services import bot_health as _bh

            rows = [{**dict(r), "health": _bh.describe(dict(r))} for r in (rows or [])]
        except Exception:
            log.debug("bots: состояние ботов недоступно uid=%s", uid)
        # Считаем то же поле, что отдаёт список (DISTINCT mb.bot_id): один бот
        # лежит отдельной строкой у каждого владельца, и счёт по строкам обещал
        # бы страницы, которых нет.
        total = await _safe_count(pool,
            f"""SELECT COUNT(DISTINCT mb.bot_id) FROM managed_bots mb
                WHERE ({_BOTS_VISIBLE_SQL})""", uid)
        return _json_resp({"bots": rows, "total": int(total or 0),
                           "offset": offset, "limit": limit})

    async def bot_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        # Проверяем доступ: личный бот ИЛИ бот из экосистемы/рабочего пространства
        owns = await _user_can_use_bot(pool, uid, bot_id)
        if not owns:
            return _err("Бот не найден или нет доступа", 404)
        bot = await _safe_fetchrow(pool,
            "SELECT * FROM managed_bots WHERE bot_id=$1", bot_id)
        if not bot:
            return _err("Бот не найден", 404)
        # Токен клиенту не отдаём. `SELECT *` тянул и его: у ботов, добавленных
        # до включения шифрования, он лежит в открытом виде (decrypt_token
        # намеренно пропускает legacy-строки как есть), то есть ответ отдавал
        # полный контроль над ботом всякому, кто видит эту выдачу — включая
        # участника рабочего пространства, которому доступ к боту дан только
        # на чтение. Заодно убираем метку уведомления — она внутренняя.
        bot = {k: v for k, v in dict(bot).items()
               if k not in ("token", "dead_notified_at")}
        try:
            from services import bot_health as _bh

            bot["health"] = _bh.describe(bot)
        except Exception:
            log.debug("bot_detail: состояние бота недоступно bot=%s", bot_id)
        subs_active = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id)
        subs_total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id)
        funnels_count = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id)
        autoreplies_count = await _safe_count(pool,
            "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=true", bot_id)
        recent_broadcasts = await _safe_fetch(pool,
            """SELECT id, message_text, status, sent_count, failed_count, total_users, created_at
               FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT 5""", bot_id)
        recent_users = await _safe_fetch(pool,
            """SELECT user_id, username, first_name, last_seen, is_active
               FROM bot_users WHERE bot_id=$1 ORDER BY last_seen DESC NULLS LAST LIMIT 10""", bot_id)
        keywords = await _safe_fetch(pool,
            "SELECT keyword, is_active FROM tracked_keywords WHERE bot_id=$1 ORDER BY created_at DESC LIMIT 10",
            bot_id)
        return _json_resp({
            "bot": bot,
            "active_subscribers": subs_active,
            "total_subscribers": subs_total,
            "active_funnels": funnels_count,
            "auto_replies": autoreplies_count,
            "recent_broadcasts": recent_broadcasts,
            "recent_users": recent_users,
            "keywords": keywords,
        })

    async def bot_auto_replies(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        owns = await _user_can_use_bot(pool, uid, bot_id)
        if not owns:
            return _err("Бот не найден", 404)
        # Счётчик срабатываний из auto_reply_log — аналитика прямо в списке.
        # LEFT JOIN, чтобы правила без срабатываний тоже вернулись (fired=0).
        # Фолбэк на простой SELECT, если новых колонок/лога ещё нет.
        try:
            rows = await pool.fetch(
                """SELECT ar.id, ar.trigger_type, ar.keyword, ar.response_text,
                          ar.is_active, ar.created_at, ar.match_mode,
                          ar.reply_delay_sec, ar.active_from_hour, ar.active_to_hour, ar.priority,
                          COALESCE(l.fired, 0) AS fired
                   FROM auto_replies ar
                   LEFT JOIN (
                       SELECT rule_id, COUNT(*) AS fired FROM auto_reply_log
                       WHERE bot_id=$1 AND rule_type='auto_reply' GROUP BY rule_id
                   ) l ON l.rule_id = ar.id
                   WHERE ar.bot_id=$1
                   ORDER BY ar.priority DESC, ar.created_at DESC""",
                bot_id)
            rows = [dict(r) for r in rows]
        except Exception:
            rows = await _safe_fetch(pool,
                "SELECT id, trigger_type, keyword, response_text, is_active, created_at, "
                "COALESCE(match_mode,'contains') AS match_mode FROM auto_replies WHERE bot_id=$1 ORDER BY created_at DESC",
                bot_id)
        return _json_resp({"auto_replies": rows})

    async def create_auto_reply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _user_can_use_bot(pool, uid, bot_id)
        if not owns:
            return _err("Бот не найден", 404)
        trigger_type = body.get("trigger_type", "keyword")
        keyword = body.get("keyword", "").strip()
        response_text = body.get("response_text", "").strip()
        # match_mode принимался из UI, но НЕ персистился → выбор exact/starts молча
        # игнорировался (правило всегда работало как contains). Валидируем и пишем.
        match_mode = (body.get("match_mode") or "contains").lower()
        if match_mode not in ("contains", "exact", "starts"):
            match_mode = "contains"
        if not response_text:
            return _err("Нужен текст ответа", 400)
        # buttons/priority/reply_delay_sec принимались из UI, но НЕ персистились
        # (тот же класс #3, что и match_mode) → инлайн-кнопки, приоритет правила и
        # человекоподобная задержка молча терялись при создании. Send-путь
        # (_rule_buttons, ORDER BY priority DESC, reply_delay_sec) их потребляет.
        buttons = []
        for b in (body.get("buttons") or [])[:10]:
            try:
                bt = validate_string(b.get("text"), max_len=64) or ""
                bu = validate_string(b.get("url"), max_len=2048) or ""
            except Exception:
                continue
            if bt and bu.lower().startswith(("http://", "https://")):
                buttons.append({"text": escape_html(bt), "url": bu})
        buttons_json = _json.dumps(buttons) if buttons else None
        try:
            priority = max(0, min(1000, int(body.get("priority") or 0)))
        except (TypeError, ValueError):
            priority = 0
        try:
            reply_delay_sec = max(0, min(300, int(body.get("reply_delay_sec") or 0)))
        except (TypeError, ValueError):
            reply_delay_sec = 0
        try:
            row = await pool.fetchrow(
                """INSERT INTO auto_replies
                       (bot_id, trigger_type, keyword, response_text, is_active,
                        match_mode, buttons, priority, reply_delay_sec)
                   VALUES ($1,$2,$3,$4,TRUE,$5,$6,$7,$8) RETURNING id""",
                bot_id, trigger_type, keyword or None, response_text, match_mode,
                buttons_json, priority, reply_delay_sec)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as e:
            log.warning("create_auto_reply bot=%d: %s", bot_id, e)
            return _err("Не удалось создать автоответ", 500)

    async def bot_funnels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        rows = await _safe_fetch(pool,
            """SELECT f.id, f.name, f.trigger_type, f.keyword, f.is_active, f.created_at,
                      COUNT(fs.id) AS total_subs,
                      COUNT(fs.id) FILTER (WHERE fs.completed=false) AS active_subs
               FROM funnels f
               LEFT JOIN funnel_subscriptions fs ON fs.funnel_id=f.id
               WHERE f.bot_id=$1
               GROUP BY f.id ORDER BY f.created_at DESC""", bot_id)
        return _json_resp({"funnels": rows})

    async def bot_subscribers(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        owns = await _user_can_use_bot(pool, uid, bot_id)
        if not owns:
            return _err("Бот не найден", 404)
        try:
            offset = max(0, int(request.query.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        rows = await _safe_fetch(pool,
            """SELECT user_id, username, first_name, last_seen, is_active, first_seen
               FROM bot_users WHERE bot_id=$1
               ORDER BY last_seen DESC NULLS LAST, user_id
               LIMIT 50 OFFSET $2""", bot_id, offset)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id)
        return _json_resp({"subscribers": rows, "total": total, "offset": offset})

    async def bot_deeplinks(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        # Имя бота — не украшение: без него ссылку нельзя собрать, а значит и
        # отдать человеку. Экран показывал «?start=promo» и кнопку удаления,
        # то есть всё, кроме самой ссылки, ради которой экран и существует.
        own = await _safe_fetchrow(pool,
            "SELECT username FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid)
        if not own:
            return _err("Бот не найден", 404)
        links = await _safe_fetch(pool,
            """SELECT id, name, start_param, click_count, unique_users, created_at
               FROM bot_deep_links WHERE bot_id=$1 ORDER BY click_count DESC LIMIT 30""", bot_id)
        total_clicks = sum(l["click_count"] or 0 for l in links)
        referrals = await _safe_count(pool,
            "SELECT COUNT(*) FROM referrals WHERE bot_id=$1", bot_id)
        for ln in links:
            if ln.get("created_at") is not None:
                ln["created_at"] = ln["created_at"].isoformat()
        return _json_resp({"links": links, "total_clicks": total_clicks,
                           "referrals": referrals,
                           "bot_username": own["username"] or ""})

    async def create_deeplink(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        name = validate_string(body.get("name"), max_len=200)
        start_param = validate_string(body.get("start_param"), max_len=64)
        if not name or not start_param:
            return _err("Нужны название и start_param")
        if not validate_start_param(start_param):
            return _err("start_param: только латиница, цифры, _ и -, не длиннее 64 символов")
        try:
            row = await pool.fetchrow(
                "INSERT INTO bot_deep_links(bot_id, name, start_param) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING id",
                bot_id, name, start_param)
            if not row:
                return _err("Такой start_param у бота уже есть")
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("create_deeplink bot=%d uid=%d", bot_id, uid)
            return _err("Не удалось создать ссылку", 500)

    async def bot_engagement(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        # Try user_activity table first, fallback to bot_users.last_seen
        # Источник важен не для отладки: кампания по когорте (target_type=cohort)
        # ВСЕГДА считает по user_activity. Если здесь сработал запасной путь по
        # bot_users, экран показал бы «🔥 142», а рассылка по той же когорте
        # нашла бы ноль получателей — и выглядело бы это как поломка рассылки.
        source = "user_activity"
        try:
            row = await pool.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '1 day') AS hot,
                     COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '7 days'
                                      AND last_seen < now()-INTERVAL '1 day') AS warm,
                     COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '30 days'
                                      AND last_seen < now()-INTERVAL '7 days') AS cold,
                     COUNT(*) FILTER (WHERE last_seen < now()-INTERVAL '30 days') AS lost,
                     COUNT(*) AS total
                   FROM user_activity WHERE bot_id=$1""", bot_id)
        except Exception as e:
            log.warning("bot_engagement user_activity uid=%d bot=%d: %s", uid, bot_id, e)
            source = "bot_users"
            try:
                row = await pool.fetchrow(
                    """SELECT
                         COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '1 day') AS hot,
                         COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '7 days'
                                          AND last_seen < now()-INTERVAL '1 day') AS warm,
                         COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '30 days'
                                          AND last_seen < now()-INTERVAL '7 days') AS cold,
                         COUNT(*) FILTER (WHERE last_seen < now()-INTERVAL '30 days') AS lost,
                         COUNT(*) AS total
                       FROM bot_users WHERE bot_id=$1""", bot_id)
            except Exception as e2:
                log.warning("bot_engagement bot_users uid=%d bot=%d: %s", uid, bot_id, e2)
                row = None
                source = "none"
        if not row:
            return _json_resp({"hot": 0, "warm": 0, "cold": 0, "lost": 0, "total": 0,
                               "source": source, "cohort_ready": False})
        out = {k: int(row[k] or 0) for k in ("hot", "warm", "cold", "lost", "total")}
        out["source"] = source
        # Кампанию по когорте можно обещать только когда числа посчитаны по
        # тому же журналу, по которому её считает движок.
        out["cohort_ready"] = source == "user_activity"
        return _json_resp(out)

    async def bot_note(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            row = await pool.fetchrow(
                "SELECT note FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception:
            log.exception("bot_note uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Не найдено", 404)
        return _json_resp({"note": row["note"] or ""})

    async def save_bot_note(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            body = await request.json()
            note = validate_string(body.get("note"), max_len=2000, required=False)
        except Exception:
            return _err("Неверное тело запроса", 400)
        try:
            res = await pool.execute(
                "UPDATE managed_bots SET note=$3 WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid, note or None,
            )
        except Exception:
            log.exception("save_bot_note uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if res == "UPDATE 0":
            return _err("Не найдено", 404)
        return _json_resp({"ok": True})

    async def bot_commands(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception:
            log.exception("bot_commands uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Не найдено", 404)
        try:
            from services import bot_api
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                cmds = await bot_api.get_my_commands(sess, row["token"])
        except Exception:
            log.exception("bot_commands get_my_commands uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"commands": cmds})

    async def set_bot_commands(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            body = await request.json()
            commands = body.get("commands", [])
            if not isinstance(commands, list):
                return _err("Команды должны быть списком", 400)
            for c in commands:
                if not isinstance(c, dict) or not c.get("command") or not c.get("description"):
                    return _err("У каждой команды должны быть имя и описание", 400)
                if len(c["command"]) > 32 or len(c["description"]) > 256:
                    return _err("Команда или описание слишком длинные", 400)
        except Exception:
            return _err("Неверное тело запроса", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception:
            log.exception("set_bot_commands uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Не найдено", 404)
        try:
            from services import bot_api
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                if commands:
                    ok = await bot_api.set_my_commands(sess, row["token"], commands)
                else:
                    ok = await bot_api.delete_my_commands(sess, row["token"])
        except Exception:
            log.exception("set_bot_commands tg_api uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if ok:
            return _json_resp({"ok": True, "count": len(commands)})
        return _err("Telegram отклонил запрос", 500)

    async def bot_profile(request: web.Request) -> web.Response:
        """Изменить профиль бота: имя, описание, краткое описание (Bot API)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        name = (body.get("name") or "").strip()[:64]
        description = (body.get("description") or "").strip()[:512]
        short_description = (body.get("short_description") or "").strip()[:120]
        if not (name or description or short_description):
            return _err("Укажите хотя бы одно поле", 400)
        row = await _safe_fetchrow(pool,
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not row:
            return _err("Бот не найден", 404)
        try:
            from services.token_vault import decrypt_token as _dt
            token = _dt(row["token"])
        except Exception:
            token = row["token"]
        import aiohttp as _aio
        results: dict = {}
        calls = []
        if name:
            calls.append(("setMyName", {"name": name}, "name"))
        if description:
            calls.append(("setMyDescription", {"description": description}, "description"))
        if short_description:
            calls.append(("setMyShortDescription", {"short_description": short_description}, "short_description"))
        try:
            async with _aio.ClientSession() as sess:
                for method, params, key in calls:
                    try:
                        async with sess.post(
                            f"https://api.telegram.org/bot{token}/{method}",
                            json=params, timeout=_aio.ClientTimeout(total=10),
                        ) as resp:
                            jd = await resp.json()
                        results[key] = bool(jd.get("ok"))
                        if not jd.get("ok"):
                            results[key + "_error"] = jd.get("description", "error")
                    except Exception as e:
                        results[key] = False
                        results[key + "_error"] = str(e)[:80]
        except Exception:
            log.exception("bot_profile uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        ok_any = any(v is True for k, v in results.items() if not k.endswith("_error"))
        return _json_resp({"ok": ok_any, "results": results})

    async def bot_avatar(request: web.Request) -> web.Response:
        """Сменить/удалить аватар бота (bot_api.set_photo/delete_my_photo — были мертвы).
        POST {photo_url}: скачать по URL (SSRF-гард) и загрузить. DELETE: удалить фото."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        row = await _safe_fetchrow(pool,
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not row:
            return _err("Бот не найден", 404)
        try:
            from services.token_vault import decrypt_token as _dt
            token = _dt(row["token"])
        except Exception:
            token = row["token"]
        from services import bot_api
        import aiohttp as _aio

        if request.method == "DELETE":
            try:
                async with _aio.ClientSession() as sess:
                    ok = await bot_api.delete_my_photo(sess, token)
                return _json_resp({"ok": bool(ok)})
            except Exception:
                log.exception("bot_avatar delete uid=%d bot=%d", uid, bot_id)
                return _err("Ошибка удаления аватара", 500)

        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        photo_url = (body.get("photo_url") or "").strip()
        # Здесь сервер скачивает по URL СРАЗУ (несколькими строками ниже),
        # поэтому нужен авторитетный гард с резолвом DNS, а не синтаксический.
        # is_safe_public_url по своей же документации ловит только литеральные
        # внутренние адреса: имя, которое резолвится в 169.254.169.254 или
        # 127.0.0.1, он пропускает — а это и есть SSRF на metadata-эндпоинт.
        if not await resolve_url_is_public(photo_url):
            return _err("Нужен публичный https URL картинки (внутренние адреса запрещены)", 400)
        try:
            async with _aio.ClientSession() as sess:
                # Скачиваем с потолком размера (5 МБ) и таймаутом.
                async with sess.get(photo_url, timeout=_aio.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return _err(f"Не удалось скачать картинку (HTTP {resp.status})", 400)
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "image" not in ctype:
                        return _err("URL не является картинкой", 400)
                    data = await resp.content.read(5 * 1024 * 1024 + 1)
                    if len(data) > 5 * 1024 * 1024:
                        return _err("Картинка слишком большая (макс. 5 МБ)", 400)
                ok = await bot_api.set_photo(sess, token, data)
            if ok:
                return _json_resp({"ok": True})
            return _err("Telegram отклонил картинку (формат/размер)", 400)
        except Exception:
            log.exception("bot_avatar set uid=%d bot=%d", uid, bot_id)
            return _err("Ошибка установки аватара", 500)

    async def bot_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            owned = await pool.fetchval(
                "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
        except Exception:
            log.exception("bot_stats uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not owned:
            return _err("Не найдено", 404)
        from database import db as _db
        try:
            stats = await _db.get_bot_stats(pool, bot_id)
            daily = await _db.get_audience_daily_growth(pool, bot_id, days=7)
        except Exception:
            log.exception("bot_stats bot=%d uid=%d", bot_id, uid)
            return _err(_INTERNAL_ERROR, 500)
        daily_list = [{"date": str(r["d"]), "count": int(r["cnt"])} for r in daily]
        return _json_resp({**stats, "daily_growth": daily_list})

    async def bot_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        token = validate_string(data.get("token"), max_len=200)
        if not token:
            return _err("token обязателен", 400)
        if not validate_bot_token(token):
            return _err("Неверный формат токена. Пример: 1234567890:AAHxxxxxxxx...", 400)
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _http:
                async with _http.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=_aio.ClientTimeout(total=10),
                ) as _resp:
                    me = await _resp.json()
            if not me.get("ok"):
                desc = me.get("description") or "неверный токен"
                return _err(f"Telegram отказал: {desc}", 400)
            bot_info = me["result"]
            bot_id = bot_info["id"]
            username = bot_info.get("username", "")
            first_name = bot_info.get("first_name", "")
            from database import db as _db
            # Лимит тарифа: enforce только для нового бота (повторное добавление
            # уже своего бота идемпотентно и не должно блокироваться).
            already_owned = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
            if not already_owned:
                from bot.utils.subscription import get_bot_limit, get_effective_bot_count
                _lim = await get_bot_limit(pool, uid)
                if await get_effective_bot_count(pool, uid) >= _lim:
                    return _err(f"Достигнут лимит ботов ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            result = await _db.add_bot(pool, token, bot_id, username, first_name, uid)
            if result == "taken":
                return _err("Этот бот уже добавлен другим пользователем", 409)
            already = (result is False)
            if already:
                # Бот уже наш — значит человек, скорее всего, вставляет НОВЫЙ
                # токен взамен отозванного. Раньше здесь просто отвечали «уже
                # добавлен», токен не обновлялся, и бот продолжал молчать; а
                # единственный обходной путь — удалить и добавить заново —
                # стирал всю аудиторию (bot_users висит на ON DELETE CASCADE).
                # bot_id тот же, поэтому подписчики, воронки и правила целы.
                replaced = await _db.replace_bot_token(
                    pool, uid, bot_id, token, username, first_name)
                return _json_resp({
                    "ok": True, "already_exists": True,
                    "token_replaced": bool(replaced),
                    "bot_id": bot_id, "username": username, "first_name": first_name,
                })
            return _json_resp({
                "ok": True, "already_exists": False,
                "bot_id": bot_id, "username": username, "first_name": first_name,
            })
        except Exception:
            log.exception("bot_add uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def bot_remove(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        # Удаление, а не пауза: бот пропадает из списков и из работы. Пауза —
        # это PUT …/toggle. Раньше этот маршрут тоже только снимал is_active, и
        # «удалённый» бот оставался в списке с кнопкой «Вернуть в работу».
        try:
            from database import db as _db

            if not await _db.delete_bot(pool, bot_id, uid):
                return _err("Бот не найден", 404)
            return _json_resp({"ok": True, "deleted": True})
        except Exception:
            log.exception("bot_remove uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def bot_webhook_info(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception:
            log.exception("bot_webhook_info uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Бот не найден", 404)
        try:
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                from services import bot_api as _bapi
                info = await _bapi.get_webhook_info(sess, row["token"])
            return _json_resp({
                "url": info.get("url", "") or "",
                "pending_update_count": info.get("pending_update_count", 0),
                "last_error_message": info.get("last_error_message", ""),
                "max_connections": info.get("max_connections", 0),
                "allowed_updates": info.get("allowed_updates", []),
                "bot_username": row["username"],
            })
        except Exception:
            log.exception("bot_webhook_info uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def bot_webhook_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
        except Exception:
            log.exception("bot_webhook_delete uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not row:
            return _err("Бот не найден", 404)
        try:
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                from services import bot_api as _bapi
                result = await _bapi.delete_webhook(sess, row["token"])
            return _json_resp({"ok": result.get("ok", False)})
        except Exception:
            log.exception("bot_webhook_delete uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def bots_scan_fleet(request: web.Request) -> web.Response:
        """Запустить скан флота на ботов (@BotFather /mybots по каждому аккаунту).

        Разрыв: скан ресурсов находил каналы и чаты, но ботов — никогда, и
        подключить бота можно было только вручную по токену. На живом флоте это
        «164 канала/чата, 32 аккаунта и 0 ботов»."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        acc_ids = [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()]
        try:
            op_id = await _obus.submit(
                pool, uid, "scan_owned_bots", {"account_ids": acc_ids},
                total_items=len(acc_ids) or 1, label="Скан флота на ботов")
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        return _json_resp({"ok": True, "op_id": op_id})

    async def bots_connect_discovered(request: web.Request) -> web.Response:
        """Подключить найденных ботов пакетом (токены забирает у @BotFather).

        Без этого скан был тупиком: найденного бота видно, а подключить нечем —
        токена у него нет. Пустой usernames = подключить всех неподключённых.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        names = [u.lstrip("@") for u in _str_list(body.get("usernames"))]
        try:
            limit = max(1, min(50, int(body.get("limit") or 20)))
        except (TypeError, ValueError):
            limit = 20
        pending = await _safe_count(pool,
            "SELECT COUNT(*) FROM discovered_bots "
            "WHERE owner_id=$1 AND linked_bot_id IS NULL AND acc_id IS NOT NULL", uid)
        if not pending:
            return _err("Нечего подключать: найденных неподключённых ботов нет — "
                        "запустите скан флота", 409)
        try:
            op_id = await _obus.submit(
                pool, uid, "connect_discovered_bots",
                {"usernames": names, "limit": limit},
                total_items=len(names) or min(int(pending), limit),
                label="Подключение найденных ботов")
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        return _json_resp({"ok": True, "op_id": op_id,
                           "pending": int(pending)})

    async def bots_enable_b2b(request: web.Request) -> web.Response:
        """Массово включить режим bot-to-bot у управляемых ботов (@BotFather).

        Нужно для сети ботов (Bot Mesh): без включённого режима боты не получают
        апдейты друг от друга. Включает только аккаунт-владелец бота — операция
        группирует ботов по владельцу сама. Первый прогон — канарейкой (limit).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            limit = max(1, min(200, int(body.get("limit") or 50)))
        except (TypeError, ValueError):
            limit = 50
        only_disabled = bool(body.get("only_disabled", True))
        cond = "AND COALESCE(b2b_enabled, FALSE)=FALSE" if only_disabled else ""
        pending = await _safe_count(pool,
            f"SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE "
            f"AND username IS NOT NULL AND acc_id IS NOT NULL {cond}", uid)
        if not pending:
            return _err("Нечего включать: у ботов либо уже включён режим, либо нет "
                        "аккаунта-владельца (тогда только вручную в @BotFather)", 409)
        try:
            op_id = await _obus.submit(
                pool, uid, "enable_bot_to_bot",
                {"limit": limit, "only_disabled": only_disabled},
                total_items=min(int(pending), limit),
                label="Включение bot-to-bot")
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        return _json_resp({"ok": True, "op_id": op_id, "pending": int(pending)})

    async def bots_discovered_list(request: web.Request) -> web.Response:
        """Найденные на флоте боты: что есть, на каком аккаунте, подключён ли."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT d.username, d.acc_id, d.linked_bot_id, d.last_seen,
                      a.first_name, a.phone
                 FROM discovered_bots d
                 LEFT JOIN tg_accounts a ON a.id = d.acc_id
                WHERE d.owner_id=$1
                ORDER BY (d.linked_bot_id IS NULL) DESC, d.username
                LIMIT 500""", uid)
        out = []
        for r in (rows or []):
            d = dict(r)
            d["last_seen"] = d["last_seen"].isoformat() if d.get("last_seen") else None
            d["connected"] = d.get("linked_bot_id") is not None
            d["account"] = d.get("first_name") or d.get("phone") or (
                f"#{d['acc_id']}" if d.get("acc_id") else "")
            out.append(d)
        return _json_resp({"ok": True, "bots": out,
                           "total": len(out),
                           "connected": sum(1 for x in out if x["connected"])})

    async def set_bot_swarm_api(request: web.Request) -> web.Response:
        """Включить или выключить бота в «Рое».

        Экран «Рой» показывал «🟢 В рою / ⚫ Выключен» и прямо отсылал владельца
        наружу: «Рой включается на карточке бота в Telegram-боте». Переключателя
        у мини-аппа не было — только у бота (`bot/handlers/swarm.py`).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        bot_id = validate_integer(request.match_info.get("bot_id"),
                                  min_val=1, max_val=2**63 - 1)
        if bot_id is None:
            return _err("Неверный идентификатор бота")
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if "enabled" not in body:
            return _err("Не указано, включать или выключать")
        enabled = bool(body["enabled"])
        res = await pool.execute(
            "UPDATE managed_bots SET swarm_enabled=$2 WHERE bot_id=$1 AND added_by=$3",
            bot_id, enabled, uid,
        )
        if res == "UPDATE 0":
            return _err("Бот не найден", 404)
        return _json_resp({"ok": True, "enabled": enabled})

    async def set_bot_role_api(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
            role = str(body.get("role", "general")).strip()
            cluster = str(body.get("cluster", "default")).strip()[:64]
        except Exception:
            return _err("Неверный запрос", 400)
        valid_roles = ("entry", "conversion", "retention", "general")
        if role not in valid_roles:
            return _err("Роль бота: entry, conversion, retention или general", 400)
        try:
            res = await pool.execute(
                "UPDATE managed_bots SET bot_role=$1, cluster=$2 WHERE bot_id=$3 AND added_by=$4",
                role, cluster, bot_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("set_bot_role_api uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def relay_sessions_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            owned = await pool.fetchval(
                "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
        except Exception:
            log.exception("relay_sessions_list ownership uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        if not owned:
            return _err("Не найдено", 404)
        try:
            rows = await pool.fetch(
                """SELECT id, user_id, username, first_name, last_activity, messages_count
                   FROM relay_sessions WHERE bot_id=$1 ORDER BY last_activity DESC LIMIT 50""",
                bot_id,
            )
        except Exception:
            log.exception("relay_sessions uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"sessions": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"] or "",
                "name": r["first_name"] or r["username"] or str(r["user_id"]),
                "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None,
                "messages_count": r["messages_count"] or 0,
            }
            for r in rows
        ]})

    async def relay_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
            enabled = bool(body.get("enabled", True))
        except Exception:
            return _err("Неверный запрос", 400)
        try:
            res = await pool.execute(
                "UPDATE managed_bots SET relay_enabled=$1 WHERE bot_id=$2 AND added_by=$3",
                enabled, bot_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True, "relay_enabled": enabled})
        except Exception:
            log.exception("relay_toggle uid=%d bot=%d", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def multigeo_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
                bot_id, uid,
            )
        except Exception as e:
            log.warning("multigeo_get db error: %s", e)
            return _err("Ошибка базы данных", 500)
        if not row:
            return _err("Бот не найден", 404)
        import aiohttp as _aiohttp
        from services import bot_api as _bapi
        langs = ["", "ru", "en", "de", "fr", "es", "it", "uk", "pt", "zh", "ar"]
        result = []
        try:
            async with _aiohttp.ClientSession() as session:
                for lc in langs:
                    try:
                        name = await _bapi.get_my_name(session, row["token"], lc)
                        desc = await _bapi.get_my_description(session, row["token"], lc)
                        short = await _bapi.get_my_short_description(session, row["token"], lc)
                        if name or desc or short:
                            result.append({"lang": lc or "default", "name": name, "description": desc, "short_description": short})
                    except Exception as e:
                        log.warning("multigeo_get get locale %s: %s", lc, e)
        except Exception:
            log.exception("multigeo_get")
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"profiles": result})

    async def multigeo_set(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        lang = (body.get("lang") or "").strip()
        name = (body.get("name") or "").strip()[:64]
        description = (body.get("description") or "").strip()[:512]
        short_description = (body.get("short_description") or "").strip()[:120]
        if lang == "default":
            lang = ""
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
                bot_id, uid,
            )
        except Exception as e:
            log.warning("multigeo_set db error: %s", e)
            return _err("Ошибка базы данных", 500)
        if not row:
            return _err("Бот не найден", 404)
        import aiohttp as _aiohttp
        from services import bot_api as _bapi
        errors = []
        try:
            async with _aiohttp.ClientSession() as session:
                if name:
                    ok = await _bapi.set_name(session, row["token"], name, lang)
                    if not ok:
                        errors.append("name")
                if description:
                    ok = await _bapi.set_description(session, row["token"], description, lang)
                    if not ok:
                        errors.append("description")
                if short_description:
                    ok = await _bapi.set_short_description(session, row["token"], short_description, lang)
                    if not ok:
                        errors.append("short_description")
        except Exception:
            log.exception("multigeo_set")
            return _err(_INTERNAL_ERROR, 500)
        if errors:
            return _json_resp({"ok": False, "errors": errors})
        return _json_resp({"ok": True})

    async def toggle_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        # Явное состояние {"active": true|false} — чтобы «поставить на паузу»
        # не превращалось в «включить», если экран показывал устаревшее
        # состояние. Без тела — прежнее переключение. Удалённого бота пауза не
        # трогает: вернуть его можно только повторным добавлением токена.
        want = None
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict) and isinstance(body.get("active"), bool):
                want = body["active"]
        try:
            row = await pool.fetchrow(
                """UPDATE managed_bots
                      SET is_active = COALESCE($3::boolean, NOT is_active)
                    WHERE bot_id=$1 AND added_by=$2 AND deleted_at IS NULL
                RETURNING bot_id, is_active""",
                bot_id, uid, want)
            if not row:
                return _err("Бот не найден", 404)
            return _json_resp({"ok": True, "is_active": row["is_active"]})
        except Exception:
            log.exception("toggle_bot")
            return _err("Не удалось переключить бота", 500)

    async def create_funnel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        name = (body.get("name") or "").strip()
        trigger_type = body.get("trigger_type", "start")
        keyword = (body.get("keyword") or "").strip() or None
        first_message = (body.get("first_message") or "").strip()
        if not name:
            return _err("Нужно название")
        if trigger_type not in ("start", "keyword"):
            return _err("Тип триггера: start или keyword")
        if trigger_type == "keyword" and not keyword:
            return _err("Для триггера по ключевому слову нужно само слово")
        if not first_message:
            return _err("Нужно первое сообщение")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    funnel_row = await conn.fetchrow(
                        "INSERT INTO funnels(bot_id, name, trigger_type, keyword) VALUES($1,$2,$3,$4) RETURNING id",
                        bot_id, name, trigger_type, keyword)
                    funnel_id = funnel_row["id"]
                    await conn.execute(
                        "INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes) VALUES($1,1,$2,0)",
                        funnel_id, first_message)
            return _json_resp({"ok": True, "funnel_id": funnel_id})
        except Exception:
            log.exception("create_funnel bot=%d uid=%d", bot_id, uid)
            return _err("Не удалось создать воронку", 500)

    async def bot_audience(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id)
        active = await _safe_count(pool, "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id)
        new_today = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '1 day'", bot_id)
        new_7d = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '7 days'", bot_id)
        new_30d = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '30 days'", bot_id)
        # Growth by day (last 14 days)
        growth = await _safe_fetch(pool,
            """SELECT DATE(first_seen) AS day, COUNT(*) AS new_users
               FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '14 days'
               GROUP BY day ORDER BY day""", bot_id)
        # Tags distribution
        tags = await _safe_fetch(pool,
            """SELECT tag, COUNT(*) AS cnt FROM user_tags WHERE bot_id=$1
               GROUP BY tag ORDER BY cnt DESC LIMIT 10""", bot_id)
        return _json_resp({
            "total": total, "active": active,
            "new_today": new_today, "new_7d": new_7d, "new_30d": new_30d,
            "growth": growth, "tags": tags,
        })

    async def bot_schedules(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор бота", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        rows = await _safe_fetch(pool,
            """SELECT id, message_text, execute_at, status, created_at
               FROM scheduled_broadcasts WHERE bot_id=$1
               ORDER BY execute_at DESC LIMIT 20""", bot_id)
        return _json_resp({"schedules": rows})

    async def create_schedule(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        text = _text(body.get("text"))
        execute_at = _text(body.get("execute_at"))
        if not text:
            return _err("Нужен текст")
        if not execute_at:
            return _err("Нужно время запуска в формате ISO")
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Бот не найден", 404)
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(execute_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                import datetime as _dt
                dt = dt.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return _err("Время запуска должно быть в формате ISO, например 2025-12-31T15:00:00Z")
        repeat_min = schedule_repeat_minutes(body.get("repeat"))
        try:
            try:
                row = await pool.fetchrow(
                    "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by, repeat_interval_min) "
                    "VALUES($1,$2,$3,$4,$5) RETURNING id",
                    bot_id, text, dt, uid, repeat_min)
            except asyncpg.UndefinedColumnError:
                # schema_v145 ещё не применена — падаем на одноразовое.
                row = await pool.fetchrow(
                    "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by) VALUES($1,$2,$3,$4) RETURNING id",
                    bot_id, text, dt, uid)
            return _json_resp({"ok": True, "id": row["id"], "repeat_min": repeat_min})
        except Exception:
            log.exception("create_schedule bot=%d uid=%d", bot_id, uid)
            return _err("Не удалось создать расписание", 500)

    async def bots_check_all(request: web.Request) -> web.Response:
        """Проверить живость всех ботов владельца: getMe по каждому токену.

        Только чтение, ничего не удаляет. Возвращает статус каждого бота
        (alive/dead/unknown), чтобы владелец видел, кого можно снести.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(
            pool,
            "SELECT bot_id, username, first_name, token FROM managed_bots "
            "WHERE added_by=$1 ORDER BY bot_id", uid)
        rows = rows or []
        if not rows:
            return _json_resp({"bots": [], "alive": 0, "dead": 0, "unknown": 0})
        from services import bot_api
        import aiohttp as _aio
        tokens = [r["token"] for r in rows]
        try:
            async with _aio.ClientSession() as sess:
                statuses = await bot_api.batch_check_alive(sess, tokens)
        except Exception:
            log.exception("bots_check_all uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        out = []
        counts = {bot_api.BOT_ALIVE: 0, bot_api.BOT_DEAD: 0, bot_api.BOT_UNKNOWN: 0}
        for r, st in zip(rows, statuses):
            counts[st] = counts.get(st, 0) + 1
            out.append({"bot_id": int(r["bot_id"]), "username": r["username"],
                        "first_name": r["first_name"], "status": st})
        return _json_resp({"bots": out,
                           "alive": counts[bot_api.BOT_ALIVE],
                           "dead": counts[bot_api.BOT_DEAD],
                           "unknown": counts[bot_api.BOT_UNKNOWN]})

    def _parse_bot_ids(raw) -> list[int] | None:
        """Список bot_id из тела запроса, либо None при негодном вводе."""
        if not isinstance(raw, list) or not raw:
            return None
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return None

    def _deleted_count(res) -> int:
        """Число строк из тега asyncpg «DELETE N» (0 при любом другом ответе)."""
        s = str(res)
        if not s.startswith("DELETE"):
            return 0
        try:
            return int(s.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def bots_bulk_delete(request: web.Request) -> web.Response:
        """Полностью удалить выбранных ботов владельца (одного или нескольких).

        В отличие от «убрать из работы» (is_active=FALSE) — это DELETE строки
        managed_bots; связанные записи уходят по ON DELETE CASCADE. Скоуп —
        только свои боты (added_by). Идемпотентно: повтор по уже удалённым = 0.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Некорректный запрос", 400)
        bot_ids = _parse_bot_ids(body.get("bot_ids"))
        if bot_ids is None:
            return _err("Не выбран ни один бот", 400)
        try:
            res = await pool.execute(
                "DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[]) AND added_by=$2",
                bot_ids, uid)
            from database.db import record_manual_action
            await record_manual_action(
                pool, uid, "bots_bulk_delete",
                target=",".join(str(b) for b in bot_ids[:50]))
            return _json_resp({"ok": True, "deleted": _deleted_count(res),
                               "requested": len(bot_ids)})
        except Exception:
            log.exception("bots_bulk_delete uid=%d n=%d", uid, len(bot_ids))
            return _err(_INTERNAL_ERROR, 500)

    async def bots_cleanup_dead(request: web.Request) -> web.Response:
        """Проверить всех ботов владельца и удалить ТОЛЬКО мёртвых (401).

        Боты со статусом unknown (сеть/Telegram недоступны) НЕ удаляются:
        удалять можно лишь то, про что точно известно, что токен отозван.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(
            pool, "SELECT bot_id, token FROM managed_bots WHERE added_by=$1", uid)
        rows = rows or []
        if not rows:
            return _json_resp({"checked": 0, "deleted": 0, "dead": 0,
                               "unknown": 0, "alive": 0, "deleted_ids": []})
        from services import bot_api
        import aiohttp as _aio
        tokens = [r["token"] for r in rows]
        try:
            async with _aio.ClientSession() as sess:
                statuses = await bot_api.batch_check_alive(sess, tokens)
        except Exception:
            log.exception("bots_cleanup_dead check uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        dead_ids = [int(r["bot_id"]) for r, st in zip(rows, statuses)
                    if st == bot_api.BOT_DEAD]
        unknown = sum(1 for st in statuses if st == bot_api.BOT_UNKNOWN)
        alive = sum(1 for st in statuses if st == bot_api.BOT_ALIVE)
        deleted = 0
        if dead_ids:
            try:
                res = await pool.execute(
                    "DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[]) AND added_by=$2",
                    dead_ids, uid)
                deleted = _deleted_count(res)
                from database.db import record_manual_action
                await record_manual_action(
                    pool, uid, "bots_cleanup_dead",
                    target=",".join(str(b) for b in dead_ids[:50]))
            except Exception:
                log.exception("bots_cleanup_dead delete uid=%d", uid)
                return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"checked": len(rows), "deleted": deleted,
                           "dead": len(dead_ids), "unknown": unknown,
                           "alive": alive, "deleted_ids": dead_ids})

    async def bots_bulk_profile(request: web.Request) -> web.Response:
        """Массово изменить профиль выбранных (или ВСЕХ) ботов владельца:
        имя, описание, короткое описание (Bot API) и приветствие на /start.

        Пустое поле — не трогаем. Пустой/отсутствующий bot_ids — все активные
        боты. Ставит операцию bulk_bot_profile (по-ботный журнал, прогресс,
        идемпотентность — в исполнителе)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Некорректный запрос", 400)

        def _field(key: str, cap: int) -> str:
            v = body.get(key)
            return "" if v is None else str(v).strip()[:cap]

        name = _field("name", 64)
        description = _field("description", 512)
        short_description = _field("short_description", 120)
        greeting = _field("greeting", 4096)
        if not (name or description or short_description or greeting):
            return _err("Укажите хотя бы одно поле: имя, описание или приветствие", 400)

        raw = body.get("bot_ids")
        bot_ids = _parse_bot_ids(raw) if raw else None  # None/пусто = все боты
        if raw and bot_ids is None:
            return _err("Неверный список ботов", 400)

        if bot_ids:
            cnt = await _safe_count(
                pool, "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 "
                "AND is_active AND bot_id = ANY($2::bigint[])", uid, bot_ids)
        else:
            cnt = await _safe_count(
                pool, "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active", uid)
        if not cnt:
            return _err("Нет подходящих ботов", 400)

        params = {"name": name, "description": description,
                  "short_description": short_description, "greeting": greeting}
        if bot_ids:
            params["bot_ids"] = bot_ids
        try:
            op_id = await _obus.submit(
                pool, uid, "bulk_bot_profile", params,
                total_items=int(cnt), label=f"Профиль ботов: {cnt} шт.")
        except _obus.PlanRequiredError as e:
            return _err(f"Доступно на тарифе «{e.required_plan}» и выше", 403)
        except Exception:
            log.exception("bots_bulk_profile uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not op_id:
            return _err("Не удалось поставить операцию", 500)
        return _json_resp({"ok": True, "op_id": op_id, "count": int(cnt)})

    async def bots_bulk_profile_rollback(request: web.Request) -> web.Response:
        """Откатить массовое изменение профиля ботов (вернуть как было).

        Берёт op_id операции применения, проверяет, что снимок принадлежит
        владельцу и ещё не откачен, и ставит операцию отката."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Некорректный запрос", 400)
        try:
            src_op_id = int(body.get("op_id") or 0)
        except (TypeError, ValueError):
            return _err("Неверная операция", 400)
        if not src_op_id:
            return _err("Не указана операция для отката", 400)
        cnt = await _safe_count(
            pool, "SELECT COUNT(*) FROM bulk_edit_snapshots "
            "WHERE owner_id=$1 AND op_id=$2 AND entity_type='bot' AND reverted_at IS NULL",
            uid, src_op_id)
        if not cnt:
            return _err("Нечего откатывать: снимок не найден или уже откачен", 400)
        try:
            op_id = await _obus.submit(
                pool, uid, "bulk_bot_profile_rollback", {"source_op_id": src_op_id},
                total_items=int(cnt), label=f"Откат профиля ботов: {cnt} шт.")
        except _obus.PlanRequiredError as e:
            return _err(f"Доступно на тарифе «{e.required_plan}» и выше", 403)
        except Exception:
            log.exception("bots_bulk_profile_rollback uid=%d src=%d", uid, src_op_id)
            return _err(_INTERNAL_ERROR, 500)
        if not op_id:
            return _err("Не удалось поставить откат", 500)
        return _json_resp({"ok": True, "op_id": op_id, "count": int(cnt)})

    app.router.add_get("/api/miniapp/bots", bots)
    app.router.add_post("/api/miniapp/bots/check_all", bots_check_all)
    app.router.add_post("/api/miniapp/bots/bulk_delete", bots_bulk_delete)
    app.router.add_post("/api/miniapp/bots/bulk_profile", bots_bulk_profile)
    app.router.add_post("/api/miniapp/bots/bulk_profile_rollback", bots_bulk_profile_rollback)
    app.router.add_post("/api/miniapp/bots/cleanup_dead", bots_cleanup_dead)
    app.router.add_post("/api/miniapp/bot/add", bot_add)
    app.router.add_delete("/api/miniapp/bot/{bot_id}", bot_remove)
    app.router.add_get("/api/miniapp/bot/{bot_id}", bot_detail)
    app.router.add_get("/api/miniapp/bot/{bot_id}/auto_replies", bot_auto_replies)
    app.router.add_post("/api/miniapp/bot/{bot_id}/auto_reply", create_auto_reply)
    app.router.add_get("/api/miniapp/bot/{bot_id}/funnels", bot_funnels)
    app.router.add_get("/api/miniapp/bot/{bot_id}/subscribers", bot_subscribers)
    app.router.add_put("/api/miniapp/bot/{bot_id}/toggle", toggle_bot)
    app.router.add_post("/api/miniapp/bot/{bot_id}/funnel", create_funnel)
    app.router.add_get("/api/miniapp/bot/{bot_id}/audience", bot_audience)
    app.router.add_get("/api/miniapp/bot/{bot_id}/schedules", bot_schedules)
    app.router.add_post("/api/miniapp/bot/{bot_id}/schedule", create_schedule)
    app.router.add_get("/api/miniapp/bot/{bot_id}/deeplinks", bot_deeplinks)
    app.router.add_post("/api/miniapp/bot/{bot_id}/deeplink", create_deeplink)
    app.router.add_get("/api/miniapp/bot/{bot_id}/engagement", bot_engagement)
    app.router.add_get("/api/miniapp/bot/{bot_id}/stats", bot_stats)
    app.router.add_get("/api/miniapp/bot/{bot_id}/note", bot_note)
    app.router.add_put("/api/miniapp/bot/{bot_id}/note", save_bot_note)
    app.router.add_get("/api/miniapp/bot/{bot_id}/commands", bot_commands)
    app.router.add_put("/api/miniapp/bot/{bot_id}/commands", set_bot_commands)
    app.router.add_post("/api/miniapp/bot/{bot_id}/profile", bot_profile)
    app.router.add_post("/api/miniapp/bot/{bot_id}/avatar", bot_avatar)
    app.router.add_delete("/api/miniapp/bot/{bot_id}/avatar", bot_avatar)
    app.router.add_put("/api/miniapp/bot/{bot_id}/role", set_bot_role_api)
    app.router.add_post("/api/miniapp/bot/{bot_id}/swarm", set_bot_swarm_api)
    app.router.add_get("/api/miniapp/bot/{bot_id}/relay/sessions", relay_sessions_list)
    app.router.add_put("/api/miniapp/bot/{bot_id}/relay/toggle", relay_toggle)
    app.router.add_get("/api/miniapp/bot/{bot_id}/multigeo", multigeo_get)
    app.router.add_post("/api/miniapp/bot/{bot_id}/multigeo", multigeo_set)
    app.router.add_post("/api/miniapp/bots/scan_fleet", bots_scan_fleet)
    app.router.add_get("/api/miniapp/bots/discovered", bots_discovered_list)
    app.router.add_post("/api/miniapp/bots/connect_discovered", bots_connect_discovered)
    app.router.add_post("/api/miniapp/bots/enable_b2b", bots_enable_b2b)
    app.router.add_get("/api/miniapp/bot/{bot_id}/webhook", bot_webhook_info)
    app.router.add_delete("/api/miniapp/bot/{bot_id}/webhook", bot_webhook_delete)
