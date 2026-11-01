"""Присутствие: пакеты, глобальное присутствие, призрачный режим.

Вынесено из `services/mini_app_api.py`: 18 маршрутов — пакеты присутствия
(`/presence_pack/*`), глобальное присутствие (`/global_presence/*`:
гео-пресеты, план, запуск, состояние) и призрачный режим (`/ghost/*`).

Вместе с ручками уехали `_presence_pack_dict`, `_ghost_fields` и словари
русских названий действий и результатов призрачного режима
(`_GHOST_ACTION_RU`, `_GHOST_RESULT_RU`): владелец читает журнал на русском, и
словари обязаны жить рядом с ручкой, которая его отдаёт.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import json

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты экранов присутствия на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _ecosystem_is_mine, _err, _get_uid, _json_resp,
        _jlist, _list_limit, _list_offset, _safe_count, _safe_fetch,
        _safe_fetchrow, log,
    )
    from services.security import validate_integer

    _GHOST_PERSONAS = ("ghost", "watcher", "active")


    _GHOST_ACTION_RU = {
        "update_status": "обновил статус «в сети»",
        "read_dialogs": "прочитал диалоги",
        "react": "поставил реакцию",
        "forward_saved": "переслал в «Избранное»",
    }
    _GHOST_RESULT_RU = {"ok": "выполнено", "skip": "пропущено", "error": "ошибка"}

    async def ghost_profiles(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT gp.id, gp.personality, gp.enabled, gp.daily_cap,
                          gp.active_hours_start, gp.active_hours_end, gp.created_at,
                          ta.phone, ta.first_name, ta.username
                   FROM ghost_profiles gp
                   JOIN tg_accounts ta ON ta.id = gp.account_id
                   WHERE gp.owner_id=$1 ORDER BY gp.created_at DESC""",
                uid,
            )
            return _json_resp({"profiles": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception:
            log.exception("ghost_profiles uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ghost_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            profile_id = int(request.match_info["profile_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор профиля", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, enabled FROM ghost_profiles WHERE id=$1 AND owner_id=$2", profile_id, uid
            )
            if not row:
                return _err("Не найдено", 404)
            new_val = not row["enabled"]
            await pool.execute(
                "UPDATE ghost_profiles SET enabled=$1, updated_at=now() WHERE id=$2 AND owner_id=$3", new_val, profile_id, uid
            )
            return _json_resp({"enabled": new_val})
        except Exception:
            log.exception("ghost_toggle uid=%d profile=%d", uid, profile_id)
            return _err(_INTERNAL_ERROR, 500)

    async def ghost_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            profile_id = int(request.match_info["profile_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор профиля", 400)
        try:
            await pool.execute(
                "DELETE FROM ghost_profiles WHERE id=$1 AND owner_id=$2", profile_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("ghost_delete uid=%d profile=%d", uid, profile_id)
            return _err(_INTERNAL_ERROR, 500)

    def _ghost_fields(data: dict) -> tuple[dict | None, str]:
        """Разобрать настройки призрачного профиля. Возвращает (поля, ошибка).

        Часы и потолок приходят из формы: без проверки диапазона в базу уезжали
        «часы 99» и «1000 действий в день», а движок потом молча не находил
        окна активности.
        """
        personality = str(data.get("personality") or "ghost").strip()
        if personality not in _GHOST_PERSONAS:
            return None, "Характер: ghost, watcher или active"
        start = validate_integer(data.get("active_hours_start", 9), min_val=0, max_val=23)
        end = validate_integer(data.get("active_hours_end", 23), min_val=0, max_val=23)
        if start is None or end is None:
            return None, "Часы активности — целые числа от 0 до 23"
        if start == end:
            return None, "Окно активности пустое: начало и конец совпадают"
        cap = validate_integer(data.get("daily_cap", 8), min_val=1, max_val=200)
        if cap is None:
            return None, "Действий в день — от 1 до 200"
        cooldown = validate_integer(data.get("cooldown_minutes", 60), min_val=1, max_val=1440)
        if cooldown is None:
            return None, "Пауза между действиями — от 1 до 1440 минут"
        return {"personality": personality, "active_hours_start": start,
                "active_hours_end": end, "daily_cap": cap,
                "cooldown_minutes": cooldown}, ""

    async def ghost_detail(request: web.Request) -> web.Response:
        """Профиль + что он реально делал. Журнал действий вёлся, но экран
        его не показывал: владелец не мог проверить, работает ли призрак."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            profile_id = int(request.match_info["profile_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор профиля", 400)
        try:
            row = await _safe_fetchrow(pool,
                """SELECT gp.*, ta.phone, ta.first_name, ta.username, ta.is_active
                     FROM ghost_profiles gp
                     JOIN tg_accounts ta ON ta.id = gp.account_id
                    WHERE gp.id=$1 AND gp.owner_id=$2""", profile_id, uid)
            if not row:
                return _err("Профиль не найден", 404)
            today = await _safe_count(pool,
                "SELECT COUNT(*) FROM ghost_action_log WHERE ghost_profile_id=$1 "
                "AND result='ok' AND executed_at >= date_trunc('day', now())", profile_id)
            limit = _list_limit(request, 30, 200)
            logs = await _safe_fetch(pool,
                """SELECT action_type, target, result, error_msg, executed_at
                     FROM ghost_action_log WHERE ghost_profile_id=$1
                    ORDER BY executed_at DESC LIMIT $2""",
                profile_id, limit)
            # Журнал приезжает последними тридцатью записями. Владелец открывает
            # его, чтобы проверить, работает ли призрак и на чём спотыкается, —
            # значит экран обязан сказать, что видит не весь журнал.
            log_total = await _safe_count(pool,
                "SELECT COUNT(*) FROM ghost_action_log WHERE ghost_profile_id=$1",
                profile_id)
            prof = dict(row)
            for key in ("created_at", "updated_at"):
                if prof.get(key):
                    prof[key] = prof[key].isoformat()
            return _json_resp({
                "profile": prof,
                "today_actions": int(today or 0),
                "log_total": int(log_total or 0),
                "limit": limit,
                "log": [{
                    "action_type": r["action_type"],
                    "action_ru": _GHOST_ACTION_RU.get(r["action_type"], r["action_type"]),
                    "target": r["target"],
                    "result": r["result"],
                    "result_ru": _GHOST_RESULT_RU.get(r["result"], r["result"]),
                    "error_msg": r["error_msg"],
                    "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
                } for r in (logs or [])],
            })
        except Exception:
            log.exception("ghost_detail uid=%d profile=%d", uid, profile_id)
            return _err(_INTERNAL_ERROR, 500)

    async def ghost_update(request: web.Request) -> web.Response:
        """Поменять настройки профиля. Раньше их можно было только пересоздать."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            profile_id = int(request.match_info["profile_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор профиля", 400)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        fields, err = _ghost_fields(data)
        if err:
            return _err(err, 400)
        try:
            upd = await _safe_fetchrow(pool,
                """UPDATE ghost_profiles
                      SET personality=$3, active_hours_start=$4, active_hours_end=$5,
                          daily_cap=$6, cooldown_minutes=$7, updated_at=now()
                    WHERE id=$1 AND owner_id=$2 RETURNING id""",
                profile_id, uid, fields["personality"], fields["active_hours_start"],
                fields["active_hours_end"], fields["daily_cap"], fields["cooldown_minutes"])
            if not upd:
                return _err("Профиль не найден", 404)
            return _json_resp({"ok": True, **fields})
        except Exception:
            log.exception("ghost_update uid=%d profile=%d", uid, profile_id)
            return _err(_INTERNAL_ERROR, 500)

    async def ghost_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        if not data.get("account_id"):
            return _err("Выберите аккаунт", 400)
        # Нечисловой id раньше доходил до `int()` внутри try и выпадал
        # пятисоткой: «внутренняя ошибка сервера» вместо «неверный аккаунт».
        account_id = validate_integer(data.get("account_id"), min_val=1)
        if not account_id:
            return _err("Неверный идентификатор аккаунта", 400)
        _f, _ferr = _ghost_fields(data)
        if _ferr:
            return _err(_ferr, 400)
        personality = _f["personality"]
        active_hours_start = _f["active_hours_start"]
        active_hours_end = _f["active_hours_end"]
        daily_cap = _f["daily_cap"]
        cooldown_minutes = _f["cooldown_minutes"]
        try:
            acc = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2", int(account_id), uid
            )
            if not acc:
                return _err("Аккаунт не найден", 404)
            pid = await pool.fetchval(
                """INSERT INTO ghost_profiles
                   (owner_id, account_id, personality, active_hours_start, active_hours_end,
                    daily_cap, cooldown_minutes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (owner_id, account_id) DO UPDATE
                     SET personality=EXCLUDED.personality,
                         active_hours_start=EXCLUDED.active_hours_start,
                         active_hours_end=EXCLUDED.active_hours_end,
                         daily_cap=EXCLUDED.daily_cap,
                         cooldown_minutes=EXCLUDED.cooldown_minutes,
                         updated_at=now()
                   RETURNING id""",
                uid, int(account_id), personality,
                active_hours_start, active_hours_end, daily_cap, cooldown_minutes,
            )
            return _json_resp({"id": pid, "ok": True})
        except Exception:
            log.exception("ghost_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def presence_map_overview(request: web.Request) -> web.Response:
        """Карта присутствия: флот по странам + активы (каналы/боты) + пробелы.

        Аккаунты по гео — geo_router; активы — счётчики; планы присутствия —
        presence_map.summarize_targets. Fail-open поблочно."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import geo_router, presence_map
        # Флот по странам.
        geo = {}
        try:
            dist = await geo_router.get_geo_distribution(pool, uid)
            geo = geo_router.summarize_distribution(dist)
            geo["by_country"] = dist
        except Exception:
            log.debug("presence_map: geo failed uid=%s", uid)
        # Страны флота с русскими названиями и раздельным счётом «всего/активных»:
        # столбик на карте показывал только активных, а открыть страну списком
        # было нечем — теперь строка ведёт в аккаунты с тем же срезом.
        countries_fleet = []
        try:
            crows = await _safe_fetch(pool,
                """SELECT COALESCE(UPPER(p.geo_country), 'UNKNOWN') AS cc,
                          COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE a.is_active) AS active
                     FROM tg_accounts a
                     LEFT JOIN user_proxies p ON p.id = a.proxy_id
                    WHERE a.owner_id = $1
                 GROUP BY cc ORDER BY total DESC""", uid)
            for r in (crows or []):
                ru = presence_map.country_ru(r["cc"])
                countries_fleet.append({**ru, "accounts": int(r["total"] or 0),
                                        "active": int(r["active"] or 0)})
        except Exception:
            log.debug("presence_map: fleet countries failed uid=%s", uid)
        # Активы.
        channels = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
        bots = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
        # Планы присутствия по странам → покрытие/пробелы.
        targets = {"countries": {}, "gaps": []}
        try:
            trows = await _safe_fetch(pool,
                """SELECT t.country, t.country_code, t.status,
                          p.id AS plan_id, p.name_pattern AS plan_name
                   FROM global_presence_targets t
                   JOIN global_presence_plans p ON p.id = t.plan_id
                   WHERE p.owner_id=$1""", uid)
            targets = presence_map.summarize_targets([dict(r) for r in trows])
        except Exception:
            log.debug("presence_map: targets failed uid=%s", uid)
        cov = presence_map.coverage_score(int(geo.get("distinct", 0) or 0),
                                          len(targets.get("gaps", [])))
        geo["countries"] = countries_fleet
        return _json_resp({"geo": geo, "assets": {"channels": channels, "bots": bots},
                           "presence": targets, "coverage": cov})

    async def presence_pack_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        target_url = (body.get("target_url") or "").strip()
        target_label = (body.get("target_label") or "").strip()
        bot_id = body.get("bot_id") or None
        if not name:
            return _err("Нужно название")
        if bot_id:
            try:
                bot_id = int(bot_id)
            except (TypeError, ValueError):
                bot_id = None
        try:
            # Через db.create_presence_pack, а не своим INSERT: там же проверка,
            # что бот наш (правка состава её делает, создание — не делало).
            from database import db as _db

            pack_id = await _db.create_presence_pack(
                pool, uid, name,
                description=description or None,
                target_url=target_url or None,
                target_label=target_label or None,
                bot_id=bot_id,
            )
            if pack_id is None:
                return _err("Бот не найден или не принадлежит вам", 404)
            return _json_resp({"ok": True, "id": pack_id})
        except Exception:
            log.exception("presence_pack_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def presence_pack_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор пакета", 400)
        try:
            await pool.execute(
                "DELETE FROM presence_packs WHERE id=$1 AND owner_id=$2", pack_id, uid
            )
            from database.db import record_manual_action

            await record_manual_action(
                pool, uid, "presence_pack_delete", target=str(pack_id))
            return _json_resp({"ok": True})
        except Exception:
            log.exception("presence_pack_delete uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    def _presence_pack_dict(pack) -> dict:
        d = dict(pack)
        d["channel_ids"] = _jlist(pack.get("channel_ids"))
        d["group_ids"] = _jlist(pack.get("group_ids"))
        return d

    async def presence_pack_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор пакета", 400)
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Пакет не найден", 404)
            return _json_resp(_presence_pack_dict(pack))
        except Exception:
            log.exception("presence_pack_detail uid=%d pack_id=%d", uid, pack_id)
            return _err(_INTERNAL_ERROR, 500)

    async def presence_pack_config(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор пакета", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Пакет не найден", 404)

            if "bot_id" in body:
                bot_id = body.get("bot_id") or None
                bot_username = None
                if bot_id:
                    try:
                        bot_id = int(bot_id)
                    except (TypeError, ValueError):
                        return _err("Неверный идентификатор бота", 400)
                    # Owner-scope: привязывать к паку можно ТОЛЬКО свой бот
                    # (added_by=uid), иначе — линковка чужого bot_id в свой пак.
                    bot_row = await pool.fetchrow(
                        "SELECT username FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                        bot_id, uid,
                    )
                    if not bot_row:
                        return _err("Бот не найден или не принадлежит вам", 404)
                    bot_username = bot_row["username"]
                await pool.execute(
                    "UPDATE presence_packs SET bot_id=$3, bot_username=$4 WHERE id=$1 AND owner_id=$2",
                    pack_id, uid, bot_id, bot_username,
                )

            if "channel_ids" in body or "group_ids" in body:
                cur = _jlist(pack.get("channel_ids"))
                grp = _jlist(pack.get("group_ids"))
                channel_ids = body.get("channel_ids", cur)
                group_ids = body.get("group_ids", grp)
                if not isinstance(channel_ids, list) or not isinstance(group_ids, list):
                    return _err("Каналы и группы должны передаваться списками", 400)
                _res = await db.update_presence_pack_channels(
                    pool, pack_id, uid,
                    [int(x) for x in channel_ids],
                    [int(x) for x in group_ids],
                )
                if not _res.get("ok"):
                    # Ровно как с чужим bot_id рядом: отказ, а не молчаливая
                    # запись чужого канала в свой пакет.
                    return _err("Канал или чат не найден или не принадлежит вам", 404)

            updated = await db.get_presence_pack(pool, pack_id, uid)
            return _json_resp(_presence_pack_dict(updated))
        except Exception:
            log.exception("presence_pack_config uid=%d pack_id=%d", uid, pack_id)
            return _err(_INTERNAL_ERROR, 500)

    async def presence_pack_seed(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор пакета", 400)
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Пакет не найден", 404)
            ch_ids = _jlist(pack.get("channel_ids"))
            if not ch_ids:
                return _err("Нет каналов в пакете", 400)
            from services.operation_bus import submit
            op_id = await submit(
                pool, uid, "seed_presence_pack", {"pack_id": pack_id},
                total_items=len(ch_ids),
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("presence_pack_seed uid=%d pack_id=%d", uid, pack_id)
            return _err(_INTERNAL_ERROR, 500)

    async def presence_pack_promote(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор пакета", 400)
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Пакет не найден", 404)
            if not pack.get("bot_id"):
                return _err("Нет бота в пакете. Привяжите бот перед назначением admin.", 400)
            ch_ids = _jlist(pack.get("channel_ids"))
            gr_ids = _jlist(pack.get("group_ids"))
            all_asset_ids = ch_ids + gr_ids
            if not all_asset_ids:
                return _err("Нет каналов/групп в пакете", 400)
            from services.operation_bus import submit
            op_id = await submit(
                pool, uid, "promote_presence_pack",
                {
                    "pack_id": pack_id,
                    "bot_tg_id": pack["bot_id"],
                    "channel_ids": all_asset_ids,
                },
                total_items=len(all_asset_ids),
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("presence_pack_promote uid=%d pack_id=%d", uid, pack_id)
            return _err(_INTERNAL_ERROR, 500)

    async def global_presence_plans(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        lim = _list_limit(request, 20, 200)
        off = _list_offset(request)
        try:
            rows = await pool.fetch(
                """
                SELECT gpp.id, gpp.asset_type, gpp.name_pattern, gpp.status,
                       gpp.created_at, gpp.updated_at,
                       COUNT(gpt.id) AS total_targets,
                       COUNT(gpt.id) FILTER (WHERE gpt.status='done') AS done_targets,
                       COUNT(gpt.id) FILTER (WHERE gpt.status='failed') AS failed_targets
                FROM global_presence_plans gpp
                LEFT JOIN global_presence_targets gpt ON gpt.plan_id = gpp.id
                WHERE gpp.owner_id=$1
                GROUP BY gpp.id, gpp.asset_type, gpp.name_pattern,
                         gpp.status, gpp.created_at, gpp.updated_at
                ORDER BY gpp.created_at DESC, gpp.id DESC
                LIMIT $2 OFFSET $3
                """,
                uid, lim, off,
            )
            # Было — голый массив из двадцати планов: экран писал «20 планов»
            # (длина страницы), остальные не открывались никак, и кнопки
            # «показать ещё» быть не могло — итога в ответе не было вовсе.
            total = await _safe_count(
                pool, "SELECT COUNT(*) FROM global_presence_plans WHERE owner_id=$1",
                uid)
            return _json_resp({
                "plans": [dict(r) for r in rows], "total": total,
                "page": {"limit": lim, "offset": off,
                         "has_more": off + len(rows) < total},
            })
        except Exception:
            log.exception("global_presence_plans uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def global_presence_plan_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        t_lim = _list_limit(request, 100, 500)
        t_off = _list_offset(request)
        try:
            plan = await pool.fetchrow(
                "SELECT * FROM global_presence_plans WHERE id=$1 AND owner_id=$2", plan_id, uid
            )
            if not plan:
                return _err("Не найдено", 404)
            targets = await pool.fetch(
                "SELECT country, region, city, language, asset_type, planned_name, status, "
                "       error_message, role, level, "
                # Фактический username: запланированный мог быть занят и
                # заменён вариантом — ссылка обязана вести на реальный объект.
                "       COALESCE(final_username, planned_username) AS username, "
                "       final_username, avatar_applied "
                "FROM global_presence_targets WHERE plan_id=$1 "
                "ORDER BY status, country, city, id LIMIT $2 OFFSET $3",
                plan_id, t_lim, t_off,
            )
            # Экран подписывал список «показаны N из M», но M брал из спека
            # (сколько целей ПЛАНИРОВАЛОСЬ), а дойти до целей за сотней было
            # нечем. Итог считает запрос, страница ходит по смещению.
            targets_total = await _safe_count(
                pool, "SELECT COUNT(*) FROM global_presence_targets WHERE plan_id=$1",
                plan_id)
            # Сводка оформления: «создано» и «оформлено» — разные числа, и
            # объект без аватара/username хуже ранжируется.
            dressing = await pool.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE status='done') AS done, "
                "       COUNT(*) FILTER (WHERE status='done' AND avatar_applied) AS with_avatar, "
                "       COUNT(*) FILTER (WHERE status='done' AND final_username IS NOT NULL) AS with_username, "
                "       COUNT(*) FILTER (WHERE status='done' AND asset_type<>'bot') AS applicable "
                "  FROM global_presence_targets WHERE plan_id=$1",
                plan_id,
            )
            from services.geo_va_link import config_from_plan
            admin_coverage = None
            if config_from_plan(plan["geo_selection"]):
                admin_coverage = await pool.fetchrow(
                    "SELECT COUNT(*) AS eligible, "
                    "COUNT(va.channel_id) AS installed, "
                    "COUNT(*) FILTER (WHERE va.enabled) AS enabled "
                    "FROM global_presence_targets t "
                    "JOIN managed_channels m ON m.owner_id=$1 AND m.channel_id=t.result_asset_id "
                    "  AND m.type='channel' "
                    "LEFT JOIN va_channel_admin va ON va.owner_id=$1 AND va.channel_id=t.result_asset_id "
                    "WHERE t.plan_id=$2 AND t.status='done'",
                    uid, plan_id,
                )
            return _json_resp(
                {
                    "plan": dict(plan),
                    "targets": [dict(t) for t in targets],
                    "targets_total": targets_total,
                    "targets_page": {"limit": t_lim, "offset": t_off,
                                     "has_more": t_off + len(targets) < targets_total},
                    "dressing": dict(dressing) if dressing else {},
                    "virtual_admin_coverage": dict(admin_coverage) if admin_coverage else None,
                }
            )
        except Exception:
            log.exception("global_presence_plan_detail uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    async def global_presence_create(request: web.Request) -> web.Response:
        """Create a new Global Presence plan with channels/groups/bots."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        
        asset_type = body.get("asset_type", "channel")
        name_pattern = (body.get("name_pattern") or "").strip()
        username_pattern = (body.get("username_pattern") or "").strip()
        description = (body.get("description") or "").strip()
        short_desc = (body.get("short_description") or "").strip()
        account_ids = body.get("account_ids") or []
        ecosystem_id = body.get("ecosystem_id")

        # ── Референс-унификация: из образца названия/username + города-образца
        # автоматически выводим паттерн ('Новости Москва' + 'Москва' → 'Новости
        # {{CITY_NAME}}'). Если явный паттерн не задан, но задан референс — деривуем.
        from services.presence_planner import (
            build_targets, derive_pattern_from_reference, render_pattern,
        )
        ref_name = (body.get("reference_name") or "").strip()
        ref_username = (body.get("reference_username") or "").strip()
        ref_city = (body.get("reference_city") or "").strip()
        ref_city_slug = (body.get("reference_city_slug") or "").strip()
        if not name_pattern and ref_name:
            name_pattern = derive_pattern_from_reference(ref_name, ref_city, "{{CITY_NAME}}")
        if not username_pattern and ref_username:
            username_pattern = derive_pattern_from_reference(
                ref_username, ref_city_slug or ref_city, "{{CITY_SLUG}}")

        if asset_type not in ("channel", "group", "bot", "package", "full_package"):
            return _err("Неверный тип ресурса: допустимы channel, group, bot, package, full_package", 400)
        if not name_pattern:
            return _err("Укажите паттерн названия или референс", 400)

        # ── Гео: пресет (страны/города мира) ИЛИ свой список городов ИЛИ страны.
        from services.geo_data import (
            GEO_PRESETS, parse_custom_geo_list, enrich_geo_list, filter_preset_cities,
        )
        geo_preset = (body.get("geo_preset") or "").strip()
        custom_cities = (body.get("custom_cities") or "").strip()
        countries = body.get("countries") or []
        preset_cities = body.get("preset_cities") or []  # выбранное подмножество городов пресета
        geo_list: list = []
        geo_source = ""
        if geo_preset and geo_preset in GEO_PRESETS:
            # Пустой preset_cities → весь пресет; иначе — только отмеченные города.
            geo_list = filter_preset_cities(geo_preset, preset_cities)
            geo_source = geo_preset
        elif custom_cities:
            geo_list = parse_custom_geo_list(custom_cities)
            geo_source = "custom"
        elif countries:
            # Фолбэк на старый ввод: страны как «города» (минимальные geo-словари).
            geo_list = enrich_geo_list([
                {"country": str(c).strip(), "city": str(c).strip()} for c in countries if str(c).strip()
            ])
            geo_source = "countries"
        if not geo_list:
            return _err("Выберите гео-пресет, свои города или страны", 400)
        from services.geo_va_link import MAX_GEO_TARGETS, validate_config
        if len(geo_list) > MAX_GEO_TARGETS:
            return _err(f"В одном плане не более {MAX_GEO_TARGETS} городов", 400)
        va_config, va_error = validate_config(body.get("virtual_admin"), asset_type)
        if va_error:
            return _err(va_error, 400)

        acc_ids_int = [int(x) for x in account_ids if str(x).isdigit()]
        account_geo: dict[int, str] = {}
        if acc_ids_int:
            # Тянем ещё и страну прокси аккаунта: build_targets привяжет цель
            # под страну X к аккаунту из страны X, когда такой есть.
            owned = await _safe_fetch(pool,
                "SELECT a.id, p.geo_country "
                "FROM tg_accounts a "
                "LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.id = ANY($2::bigint[])",
                uid, acc_ids_int)
            acc_ids_int = [int(r["id"]) for r in (owned or [])]
            account_geo = {int(r["id"]): (r.get("geo_country") or "")
                           for r in (owned or [])}

        # Генерируем цели тем же конвейером, что и бот (build_targets → рендер
        # плейсхолдеров per-city). Раньше mini-app путь цели НЕ создавал вообще —
        # план ставился в очередь, а исполнитель находил 0 целей.
        targets = build_targets(
            geo_list,
            "channel" if asset_type in ("package", "full_package") else asset_type,
            name_pattern, username_pattern or None, acc_ids_int,
            account_geo=account_geo,
        )
        if not targets:
            return _err("Не удалось сгенерировать цели из гео", 400)
        # Превью первых названий/username для ответа.
        preview = [
            {"name": t.get("planned_name"), "username": t.get("planned_username")}
            for t in targets[:5]
        ]

        try:
            # status='pending' — иначе launch (требует pending/failed) отклонял бы 'draft'.
            plan_id = await pool.fetchval(
                """INSERT INTO global_presence_plans
                   (owner_id, asset_type, name_pattern, username_pattern,
                    geo_selection, account_selection, status)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'pending')
                   RETURNING id""",
                uid, asset_type, name_pattern, username_pattern,
                json.dumps({"geo_source": geo_source, "count": len(targets),
                            "description": description, "short_description": short_desc,
                            "virtual_admin": va_config}),
                json.dumps({"account_ids": acc_ids_int}),
            )
            from database.db import create_global_presence_targets
            await create_global_presence_targets(pool, plan_id, targets)
            # Привязка к экосистеме — с той же проверкой владения, что и в
            # фабрике ботов: номер экосистемы приходит от клиента, а запись в
            # чужую экосистему означает запись в чужую структуру доступа.
            if ecosystem_id:
                try:
                    if not await _ecosystem_is_mine(pool, ecosystem_id, uid):
                        from database.db import record_manual_action

                        await record_manual_action(
                            pool, uid, "ecosystem_foreign_link_refused",
                            target=f"eco:{ecosystem_id} plan:{plan_id}",
                            result="refused")
                        return _err("Экосистема не найдена", 404)
                    await pool.execute(
                        "INSERT INTO ecosystem_global_presence (ecosystem_id, plan_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        int(ecosystem_id), plan_id,
                    )
                except Exception as e:
                    log.warning("global_presence_create ecosystem link: %s", e)
            return _json_resp({
                "ok": True, "plan_id": plan_id, "asset_type": asset_type,
                "name_pattern": name_pattern, "username_pattern": username_pattern,
                "targets": len(targets), "geo_source": geo_source, "preview": preview,
            })
        except Exception:
            log.exception("global_presence_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def global_presence_launch(request: web.Request) -> web.Response:
        """Launch a Global Presence plan — creates targets and queues operations."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        try:
            plan = await pool.fetchrow(
                "SELECT * FROM global_presence_plans WHERE id=$1 AND owner_id=$2", plan_id, uid
            )
            if not plan:
                return _err("План не найден", 404)
            if plan["status"] not in ("pending", "failed"):
                return _err("План уже запущен или завершён", 400)
            
            # Queue the operation
            from services import operation_bus
            op_type = f"global_presence_{plan['asset_type']}"
            op_id = await operation_bus.submit(
                pool, uid, op_type,
                {"plan_id": plan_id, "asset_type": plan["asset_type"]},
                total_items=0,
            )
            return _json_resp({"ok": True, "op_id": op_id, "plan_id": plan_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("global_presence_launch uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    async def global_presence_va_retry(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        try:
            plan = await pool.fetchrow(
                "SELECT geo_selection FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
                plan_id, uid,
            )
            if not plan:
                return _err("План не найден", 404)
            from services.geo_va_link import config_from_plan, retry_missing
            config = config_from_plan(plan["geo_selection"])
            if not config:
                return _err("В этом плане администратор не настроен", 400)
            result = await retry_missing(pool, uid, plan_id, config)
            return _json_resp({"ok": True, **result})
        except Exception:
            log.exception("global_presence_va_retry uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/ghost", ghost_profiles)
    app.router.add_post("/api/miniapp/ghost", ghost_create)
    app.router.add_get("/api/miniapp/ghost/{profile_id}", ghost_detail)
    app.router.add_put("/api/miniapp/ghost/{profile_id}", ghost_update)
    app.router.add_put("/api/miniapp/ghost/{profile_id}/toggle", ghost_toggle)
    app.router.add_delete("/api/miniapp/ghost/{profile_id}", ghost_delete)
    app.router.add_get("/api/miniapp/presence/map", presence_map_overview)
    app.router.add_post("/api/miniapp/presence_pack", presence_pack_create)
    app.router.add_delete("/api/miniapp/presence_pack/{pack_id}", presence_pack_delete)
    app.router.add_get("/api/miniapp/presence_pack/{pack_id}", presence_pack_detail)
    app.router.add_put("/api/miniapp/presence_pack/{pack_id}/config", presence_pack_config)
    app.router.add_post("/api/miniapp/presence_pack/{pack_id}/seed", presence_pack_seed)
    app.router.add_post("/api/miniapp/presence_pack/{pack_id}/promote", presence_pack_promote)
    app.router.add_get("/api/miniapp/global_presence", global_presence_plans)
    app.router.add_get("/api/miniapp/global_presence/{plan_id}", global_presence_plan_detail)
    app.router.add_post("/api/miniapp/global_presence", global_presence_create)
    app.router.add_post("/api/miniapp/global_presence/{plan_id}/launch", global_presence_launch)
    app.router.add_post("/api/miniapp/global_presence/{plan_id}/virtual_admin/retry", global_presence_va_retry)
