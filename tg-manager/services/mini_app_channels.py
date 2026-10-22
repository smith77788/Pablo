"""Каналы и группы владельца — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 15 маршрутов `/api/miniapp/channel/*`
и `/api/miniapp/channels/*` и 16 функций. Вместе с ними уехало замыкание
`_mass_membership_op` — массовое вступление и выход флотом; его единственные
потребители это `channels_bulk_join` и `channels_bulk_leave`.

В монолите остались `channels_bulk_edit` и `channels_import_all`: это
двухстрочные обёртки над ОБЩИМИ замыканиями `_bulk_edit_op` и `_import_all_op`,
которыми пользуются ещё боты, профили аккаунтов и группы — переносить вместе с
ними пришлось бы и их.

Здесь — только разбор запроса, скоуп владельца, русские ошибки и постановка
операции через `operation_bus`; работа с Telegram живёт в
`services/account_manager.py` и `services/channel_*`. Поведение при переносе не
менялось: тела обработчиков и строки регистрации маршрутов перенесены дословно.
"""
from __future__ import annotations

import re

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты каналов и групп на приложении мини-аппа."""
    from services import operation_bus as _obus
    from services.mini_app_api import (
        _CHANNEL_IS_GROUP_SQL, _CHANNELS_VISIBLE_SQL, _INTERNAL_ERROR,
        _cached_user, _channel_filter_sql, _err, _get_uid, _json_resp,
        _safe_count, _safe_fetch, _safe_fetchrow, channel_edit_worker_op, log,
    )
    from services.security import (
        check_sql_suspicious, validate_integer, validate_string,
    )

    # Режимы задержки массового вступления. Единственный потребитель —
    # `_mass_membership_op`, уехавший сюда вместе с ручками.
    _JOIN_DELAY_MODES = ("fast", "normal", "slow", "smart")

    @_cached_user()
    async def channels(request: web.Request) -> web.Response:
        """Каналы владельца: страница + срез + поиск + честные счётчики срезов.

        Поиск и срезы (каналы/группы/мои/чужие) раньше считались на клиенте по
        уже загруженным страницам. У кого каналов больше страницы, поиск по
        каналу со следующей страницы не находил ничего, а чип «👑 Мои 500»
        показывал не сколько их, а сколько успело загрузиться — рядом с
        заголовком, где стояло настоящее общее число.
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
        kind = q.get("kind") if q.get("kind") in ("channel", "group") else None
        role = q.get("role") if q.get("role") in ("mine", "foreign") else None
        search = (q.get("search") or "").strip()[:100] or None

        cond, extra = _channel_filter_sql(kind, role, search, next_param=4)
        rows = await _safe_fetch(pool,
            f"""SELECT DISTINCT channel_id AS id, channel_id, username, title,
                      COALESCE(members_count, 0) AS member_count,
                      type, added_at, is_admin, is_creator,
                      CASE
                          WHEN is_creator THEN 'owner'
                          WHEN is_admin THEN 'admin'
                          WHEN is_admin IS FALSE THEN 'member'
                          ELSE 'unknown'
                      END AS role
               FROM managed_channels
               WHERE ({_CHANNELS_VISIBLE_SQL}){cond}
               ORDER BY member_count DESC NULLS LAST
               LIMIT $2 OFFSET $3""", uid, limit, offset, *extra)
        # Считаем ТО ЖЕ, что показывает список: он отдаёт DISTINCT channel_id, а
        # счётчик брал DISTINCT id (первичный ключ строки). Один и тот же канал
        # лежит в managed_channels отдельной строкой у каждого владельца, а выборка
        # тянет ещё и чужие строки — из экосистем и рабочих пространств. Общий
        # канал попадал в список один раз, а в счётчик — столько раз, сколько у
        # него владельцев: шапка писала «47 каналов» там, где их 40, и кнопка
        # «Загрузить ещё» предлагала догрузить то, чего нет. То же правило — и в
        # счётчиках чипов ниже: они стоят рядом с этой же шапкой.
        #
        # Сколько каналов отвечает ТЕКУЩЕМУ срезу: по нему считается «загрузить
        # ещё», и он обязан совпадать с тем, что реально отдаётся страницами.
        cond_total, extra_total = _channel_filter_sql(kind, role, search, next_param=2)
        total = await _safe_count(pool,
            f"""SELECT COUNT(DISTINCT channel_id) FROM managed_channels
                WHERE ({_CHANNELS_VISIBLE_SQL}){cond_total}""", uid, *extra_total)
        # Счётчики чипов — по ВСЕМ видимым каналам (и с учётом поиска, если он
        # есть), одним запросом вместо подсчёта загруженного на клиенте.
        cond_counts, extra_counts = _channel_filter_sql(None, None, search, next_param=2)
        crow = await _safe_fetchrow(pool,
            f"""SELECT COUNT(DISTINCT channel_id) AS all_n,
                       COUNT(DISTINCT channel_id) FILTER (WHERE {_CHANNEL_IS_GROUP_SQL}) AS group_n,
                       COUNT(DISTINCT channel_id) FILTER (WHERE NOT {_CHANNEL_IS_GROUP_SQL}) AS channel_n,
                       COUNT(DISTINCT channel_id) FILTER (WHERE is_creator IS TRUE) AS mine_n,
                       COUNT(DISTINCT channel_id) FILTER (WHERE is_admin IS FALSE) AS foreign_n
                  FROM managed_channels
                 WHERE ({_CHANNELS_VISIBLE_SQL}){cond_counts}""", uid, *extra_counts)
        counts = {"all": 0, "channel": 0, "group": 0, "mine": 0, "foreign": 0}
        if crow:
            counts = {"all": int(crow["all_n"] or 0),
                      "channel": int(crow["channel_n"] or 0),
                      "group": int(crow["group_n"] or 0),
                      "mine": int(crow["mine_n"] or 0),
                      "foreign": int(crow["foreign_n"] or 0)}
        return _json_resp({"channels": rows, "total": int(total or 0),
                           "counts": counts, "offset": offset, "limit": limit})

    async def post_to_channel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        text = validate_string(body.get("text"), max_len=4096)
        if not text:
            return _err("Нужен текст")
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте поста")
        if len(text) > 4096:
            return _err("Сообщение длиннее 4096 символов")
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
        # Отложенная публикация: schedule_minutes минут от текущего момента.
        try:
            schedule_minutes = max(0, min(int(body.get("schedule_minutes") or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            schedule_minutes = 0
        sched = None
        if schedule_minutes > 0:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            sched = (_dt.now(_tz.utc) + _td(minutes=schedule_minutes)).isoformat()
        try:
            from services.operation_bus import submit
            # Контракт _exec_bulk_post_to_channel: account_ids[], channel_ref (числовой
            # channel_id), text_to_post, bulk_access_hash. Раньше слали channel_id/
            # account_id/text — воркер их не читал и пост в канал ничего не делал.
            op_id = await submit(pool, uid, "bulk_post_to_channel", {
                "account_ids": [int(ch["acc_id"])],
                "channel_ref": int(ch_id),
                "text_to_post": text,
                "bulk_access_hash": int(ch.get("access_hash") or 0),
            }, total_items=1, scheduled_for=sched)
            return _json_resp({"ok": True, "op_id": op_id, "scheduled_minutes": schedule_minutes})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("post_to_channel ch=%d uid=%d", ch_id, uid)
            return _err("Не удалось поставить публикацию в очередь", 500)

    async def pin_channel_last_post(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор канала", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels "
            "WHERE channel_id=$1 AND owner_id=$2", ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "pin_last_post", {
                "channel_ref": int(ch_id),
                "account_id": int(ch["acc_id"]),
                "access_hash": int(ch.get("access_hash") or 0),
            }, total_items=1)
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("pin_channel_last_post ch=%d uid=%d", ch_id, uid)
            return _err("Не удалось поставить закрепление в очередь", 500)

    async def channel_invite_link(request: web.Request) -> web.Response:
        """Сгенерировать/получить инвайт-ссылку канала (инлайн, один Telethon-вызов).
        Возможность account_manager.get_channel_invite_link раньше была недоступна в UI."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор канала", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels "
            "WHERE channel_id=$1 AND owner_id=$2", ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", int(ch["acc_id"]), uid)
        if not acc or not acc.get("session_str"):
            return _err("Привязанный аккаунт недоступен", 400)
        try:
            from services import account_manager
            link = await account_manager.get_channel_invite_link(
                acc["session_str"], int(ch_id), _acc=dict(acc),
                access_hash=int(ch.get("access_hash") or 0),
            )
            if not link:
                return _err("Не удалось получить ссылку (нужны права администратора у аккаунта)", 400)
            return _json_resp({"ok": True, "invite_link": link, "title": ch.get("title") or ""})
        except Exception:
            log.exception("channel_invite_link ch=%d uid=%d", ch_id, uid)
            return _err("Ошибка получения ссылки", 500)

    async def channel_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор канала", 400)
        ch = await _safe_fetchrow(pool,
            """SELECT channel_id, username, title, type,
                      COALESCE(members_count, 0) AS member_count,
                      acc_id, added_at
               FROM managed_channels WHERE channel_id=$1 AND owner_id=$2""", ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        acc = None
        if ch and ch.get("acc_id"):
            acc = await _safe_fetchrow(pool,
                "SELECT id, phone, first_name, username FROM tg_accounts WHERE id=$1",
                ch["acc_id"])
        recent_ops = await _safe_fetch(pool,
            """SELECT op_type, status, done_items, total_items, created_at
               FROM operation_queue WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 8""", uid)
        # ── Статистика роста участников (снимок за день + дельты) ─────────────
        mc = int(ch.get("member_count") or 0)
        if mc > 0:
            try:
                await pool.execute(
                    """INSERT INTO channel_member_history(owner_id, channel_id, members_count)
                       VALUES($1,$2,$3)
                       ON CONFLICT (owner_id, channel_id, captured_on)
                       DO UPDATE SET members_count=EXCLUDED.members_count, captured_at=NOW()""",
                    uid, ch_id, mc)
            except Exception:
                log.debug("channel_detail: member snapshot failed ch=%d", ch_id)
        hist = await _safe_fetch(pool,
            """SELECT members_count, captured_on FROM channel_member_history
               WHERE owner_id=$1 AND channel_id=$2
               ORDER BY captured_on DESC LIMIT 30""", uid, ch_id)
        def _delta(days):
            from datetime import date as _date, timedelta as _tdd
            cutoff = _date.today() - _tdd(days=days)
            for r in hist:
                if r["captured_on"] <= cutoff:
                    return mc - int(r["members_count"])
            return None
        member_growth = {
            "current": mc,
            "points": len(hist),
            "d1": _delta(1),
            "d7": _delta(7),
            "d30": _delta(30),
            "series": [
                {"d": r["captured_on"].isoformat(), "c": int(r["members_count"])}
                for r in reversed(hist)
            ],
        }
        # Публикаций через нас — из channel_service (единственный источник этой метрики).
        posts_published = 0
        try:
            from services import channel_service
            stats = await channel_service.get_channel_stats(pool, ch_id, uid)
            posts_published = int(stats.get("posts_published") or 0)
        except Exception:
            log.debug("channel_detail: posts_published failed ch=%d", ch_id)
        return _json_resp({
            "channel": ch,
            "linked_account": acc,
            "recent_ops": recent_ops,
            "member_growth": member_growth,
            "posts_published": posts_published,
        })

    async def channel_edit(request: web.Request) -> web.Response:
        """Изменить название/описание/username канала (op: title|about|username).
        Маппится на op_type=bulk_chan_exec (per-channel пара)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        op = body.get("op")
        value = (body.get("value") or "").strip()
        worker_op = channel_edit_worker_op(op)
        if not worker_op or not value:
            return _err("Укажите op (title|about|username) и значение", 400)
        if op == "title" and len(value) > 128:
            return _err("Название канала — до 128 символов", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
        params = {
            "op": worker_op,
            "value": value,
            "base_uname": value,
            "channel_acc_pairs": [{"channel_id": ch_id, "acc_id": int(ch["acc_id"]), "title": ch.get("title") or ""}],
        }
        try:
            op_id = await _obus.submit(
                pool, uid, "bulk_chan_exec", params,
                total_items=1, label=f"Канал: {op}")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channel_edit uid=%d ch=%d op=%s", uid, ch_id, op)
            return _err(_INTERNAL_ERROR, 500)

    async def channel_promote(request: web.Request) -> web.Response:
        """Назначить все аккаунты администраторами канала (promote_all_admins)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор канала", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, acc_id FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта (создателя)", 400)
        params = {"channel_id": ch_id, "owner_acc_id": int(ch["acc_id"])}
        try:
            op_id = await _obus.submit(
                pool, uid, "promote_all_admins", params,
                total_items=1, label="Назначение админов")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channel_promote uid=%d ch=%d", uid, ch_id)
            return _err(_INTERNAL_ERROR, 500)

    async def channels_mass(request: web.Request) -> web.Response:
        """Массовое действие над выбранными каналами.
        body: {op: post|about|username|promote, channel_ids: [..], value?/text?}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        op = body.get("op")
        ids_in = [int(x) for x in (body.get("channel_ids") or []) if str(x).lstrip("-").isdigit()]
        if not ids_in:
            return _err("Выберите каналы", 400)
        chans = await _safe_fetch(pool,
            "SELECT channel_id, title, acc_id, access_hash, username FROM managed_channels "
            "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])", uid, ids_in)
        chans = [c for c in (chans or []) if c.get("acc_id")]
        if not chans:
            return _err("Каналы не найдены или нет привязанного аккаунта", 404)
        n = len(chans)
        try:
            if channel_edit_worker_op(op):
                value = (body.get("value") or "").strip()
                if not value:
                    return _err("Укажите значение", 400)
                if op == "title" and len(value) > 128:
                    return _err("Название канала — до 128 символов", 400)
                if op == "title" and len(chans) != 1:
                    return _err("Название можно менять только у одного явно выбранного канала", 400)
                worker_op = channel_edit_worker_op(op)
                pairs = [{"channel_id": int(c["channel_id"]), "acc_id": int(c["acc_id"]),
                          "title": c.get("title") or "",
                          "access_hash": int(c.get("access_hash") or 0),
                          "username": c.get("username") or ""} for c in chans]
                params = {"op": worker_op, "value": value, "base_uname": value,
                          "channel_acc_pairs": pairs}
                op_id = await _obus.submit(
                    pool, uid, "bulk_chan_exec", params,
                    total_items=n, label=f"Каналы ({op}): {n}")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            if op == "post":
                text = (body.get("text") or "").strip()
                if not text:
                    return _err("Введите текст поста", 400)
                if len(text) > 4096:
                    return _err("Слишком длинный текст (макс. 4096)", 400)
                # ОДНА операция на все каналы, а не по одной на канал. Цикл
                # submit плодил десятки «Массовая публикация в канал» в
                # диспетчере: у каждой свой предохранитель и пейсинг, общий
                # разнос (в т.ч. анти-детект ссылок) ломался, а прогресс и отчёт
                # дробились. mass_publish публикует в channel_ids от управляющего
                # аккаунта КАЖДОГО канала (mc.acc_id) одной операцией — с единым
                # пейсингом, идемпотентностью повтора, spintax и варьированием
                # ссылок. target="all": пользователь выбрал каналы явно, среди
                # них могут быть и группы — по типу не отсекаем.
                acc_ids = sorted({int(c["acc_id"]) for c in chans})
                ch_ids = [int(c["channel_id"]) for c in chans]
                op_id = await _obus.submit(
                    pool, uid, "mass_publish",
                    {"text": text, "channel_ids": ch_ids,
                     "account_ids": acc_ids, "target": "all"},
                    total_items=n, label=f"Публикация в каналы: {n}")
                return _json_resp(
                    {"ok": True, "op_id": op_id, "op_ids": [op_id], "count": n})
            if op == "promote":
                op_ids = []
                for c in chans:
                    oid = await _obus.submit(
                        pool, uid, "promote_all_admins",
                        {"channel_id": int(c["channel_id"]),
                         "owner_acc_id": int(c["acc_id"])},
                        total_items=1,
                        label=f"Админы: {c.get('title') or c['channel_id']}")
                    op_ids.append(int(oid))
                return _json_resp({"ok": True, "op_ids": op_ids, "count": n})
            return _err("Неизвестная операция", 400)
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channels_mass uid=%d op=%s", uid, op)
            return _err(_INTERNAL_ERROR, 500)

    async def channel_add(request: web.Request) -> web.Response:
        """Вступить в существующий канал/группу по ссылке и добавить в управление."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверное тело запроса", 400)
        link = validate_string(body.get("channel_identifier"), max_len=500)
        if not link:
            return _err("Нужен идентификатор канала")
        # Validate URL format for channel identifier
        if not link.startswith(("http", "@", "t.me")):
            return _err("Неверный формат идентификатора канала")
        try:
            from bot.utils.subscription import get_channel_limit, get_effective_channel_count
            _lim = await get_channel_limit(pool, uid)
            if await get_effective_channel_count(pool, uid) >= _lim:
                return _err(f"Достигнут лимит каналов ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            op_id = await _obus.submit(
                pool, uid, "channel_add", {"channel_identifier": link},
                total_items=1, label=f"Добавить канал: {link}")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channel_add uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def channel_remove(request: web.Request) -> web.Response:
        """Убрать канал из управления (запись managed_channels)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор канала", 400)
        try:
            res = await pool.execute(
                "DELETE FROM managed_channels WHERE channel_id=$1 AND owner_id=$2", ch_id, uid)
            if str(res).endswith(" 0"):
                return _err("Канал не найден", 404)
            # Канал — владеемый ресурс, и убрать его может не только хозяин:
            # доступ даётся и через workspace, и через экосистему. Без записи
            # на вопрос «куда делся канал» в системе нет ответа.
            from database.db import record_manual_action

            await record_manual_action(
                pool, uid, "channel_remove", target=str(ch_id))
            return _json_resp({"ok": True})
        except Exception:
            log.exception("channel_remove uid=%d ch=%d", uid, ch_id)
            return _err(_INTERNAL_ERROR, 500)

    async def channels_bulk_delete(request: web.Request) -> web.Response:
        """Убрать из управления несколько каналов разом (паритет с channel_remove).

        DELETE строк managed_channels по owner-скоупу. Это Infragram-side: сам
        канал в Telegram не трогаем, лишь перестаём им управлять. Идемпотентно:
        повтор по уже убранным = 0, а не ошибка.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Некорректный запрос", 400)
        raw = body.get("channel_ids")
        if not isinstance(raw, list) or not raw:
            return _err("Не выбран ни один канал", 400)
        try:
            ch_ids = [int(x) for x in raw]
        except (TypeError, ValueError):
            return _err("Неверный идентификатор канала", 400)
        try:
            res = await pool.execute(
                "DELETE FROM managed_channels WHERE channel_id = ANY($1::bigint[]) "
                "AND owner_id=$2", ch_ids, uid)
            deleted = int(str(res).rsplit(" ", 1)[-1]) if str(res).startswith("DELETE") else 0
            from database.db import record_manual_action

            await record_manual_action(
                pool, uid, "channels_bulk_delete",
                target=",".join(str(c) for c in ch_ids[:50]))
            return _json_resp({"ok": True, "deleted": deleted, "requested": len(ch_ids)})
        except Exception:
            log.exception("channels_bulk_delete uid=%d n=%d", uid, len(ch_ids))
            return _err(_INTERNAL_ERROR, 500)

    async def channels_bulk_create(request: web.Request) -> web.Response:
        """Массовое создание каналов аккаунтом (паритет с ботом).

        Контракт параметров совпадает с ботовым путём (bot/handlers —
        bulk_create_confirm), чтобы не плодить второй источник правды.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        # ── Мульти-аккаунт: создаём у КАЖДОГО выбранного аккаунта (Фабрика в
        # мини-аппе). Тот же op bulk_create_channels — при наличии account_ids
        # исполнитель уходит в multi-путь (_exec_bulk_create_channels_multi). ──
        raw_ids = data.get("account_ids")
        if raw_ids:
            try:
                acc_ids = [int(x) for x in raw_ids][:200]
            except (TypeError, ValueError):
                return _err("account_ids должен быть списком чисел", 400)
            acc_ids = [a for a in acc_ids if a > 0]
            if not acc_ids:
                return _err("Выберите хотя бы один аккаунт", 400)
            title = validate_string(data.get("title") or data.get("prefix"), max_len=128)
            if not title:
                return _err("Укажите название", 400)
            about = validate_string(data.get("about"), max_len=255) or ""
            channel_count = min(max(validate_integer(data.get("channel_count", 1), min_val=1, max_val=20) or 1, 1), 20)
            name_mode = (data.get("name_mode") or "num")
            if name_mode not in ("num", "acc", "none", "keywords"):
                name_mode = "num"
            # SEO-массив: имя как набор ключей (перестановки) + шаблон @юзернейма,
            # который тоже вращается по ключам (@Dostavka_Moskva → @Moskva_Dostavka,
            # @Dostavkaj_Moskva …). Публичные @ занимают много мест в поиске.
            username_template = validate_string(data.get("username_template"), max_len=64) or ""
            # Автопосев + первый контент — настраиваются ДО старта (автоматизация).
            first_post = validate_string(data.get("first_post"), max_len=4000) or ""
            pin_first_post = bool(data.get("pin_first_post"))
            seed_count = min(max(validate_integer(data.get("seed_count", 0), min_val=0, max_val=200) or 0, 0), 200)
            engage_count = min(max(validate_integer(data.get("engage_count", 0), min_val=0, max_val=200) or 0, 0), 200)
            is_group = bool(data.get("is_group"))
            # Оставляем только СВОИ активные аккаунты с сессией.
            rows = await _safe_fetch(
                pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[]) "
                "AND is_active=TRUE AND session_str IS NOT NULL",
                uid, acc_ids)
            owned = [int(r["id"]) for r in (rows or [])]
            if not owned:
                return _err("Нет доступных активных аккаунтов из выбранных", 400)
            total = len(owned) * channel_count
            try:
                from services import operation_bus
                op_id = await operation_bus.submit(
                    pool, uid, "bulk_create_channels",
                    {
                        "account_ids": owned,
                        "title": title,
                        "about": about,
                        "channel_count": channel_count,
                        "name_mode": name_mode,
                        "username_template": username_template,
                        "first_post": first_post,
                        "pin_first_post": pin_first_post,
                        "seed_count": seed_count,
                        "engage_count": engage_count,
                        "is_group": is_group,
                    },
                    total_items=total,
                    label=f"Массовое создание {'групп' if is_group else 'каналов'}: "
                          f"{total} шт. на {len(owned)} акк.",
                )
                return _json_resp({"ok": True, "op_id": op_id, "count": total})
            except PermissionError as exc:
                return _err(str(exc) or "Требуется подписка", 403)
            except Exception:
                log.exception("channels_bulk_create(multi) uid=%d", uid)
                return _err(_INTERNAL_ERROR, 500)

        acc_id = validate_integer(data.get("acc_id"), min_val=1)
        if not acc_id:
            return _err("Выберите аккаунт, который создаст каналы", 400)
        prefix = validate_string(data.get("prefix"), max_len=128)
        if not prefix:
            return _err("Укажите название/префикс канала", 400)
        about = validate_string(data.get("about"), max_len=255) or ""
        uname_pattern = validate_string(data.get("username_pattern"), max_len=32) or ""
        count = min(max(validate_integer(data.get("count", 1), min_val=1, max_val=20) or 1, 1), 20)

        acc = await _safe_fetchrow(
            pool,
            "SELECT id, is_active, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            int(acc_id), uid,
        )
        if not acc:
            return _err("Аккаунт не найден", 404)
        if not acc["is_active"] or not acc["session_str"]:
            return _err("Аккаунт неактивен или без сессии — выберите другой", 400)

        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bulk_create_channels",
                {
                    "prefix": prefix,
                    "count": count,
                    "about": about,
                    "username_pattern": uname_pattern,
                    "acc_id": int(acc_id),
                },
                total_items=count,
                label=f"Создание каналов: {count} шт. через аккаунт #{acc_id}",
            )
            return _json_resp({"ok": True, "op_id": op_id, "count": count})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channels_bulk_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _mass_membership_op(request: web.Request, kind: str) -> web.Response:
        """Массовое вступление/выход по списку чатов (паритет с ботом).

        kind='join'  → op bulk_join,  список зовётся "links";
        kind='leave' → op bulk_leave, список зовётся "channels".
        Имена параметров и total_items = цели × аккаунты повторяют ботовый путь
        (bot/handlers/mass_ops.py) — второй источник правды не плодим.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        raw = data.get("targets") or data.get("links") or data.get("channels") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,;]+", raw)
        targets = [t.strip() for t in raw if isinstance(t, str) and t.strip()][:500]
        if not targets:
            return _err("Укажите хотя бы один канал/группу", 400)

        acc_ids = [int(x) for x in (data.get("account_ids") or []) if str(x).isdigit()]
        if not acc_ids:
            return _err("Выберите хотя бы один аккаунт", 400)

        delay_mode = str(data.get("delay_mode") or "smart")
        if delay_mode not in _JOIN_DELAY_MODES:
            delay_mode = "smart"

        # Аккаунты — только свои и живые, иначе операция провалится уже в воркере.
        rows = await _safe_fetch(
            pool,
            "SELECT id FROM tg_accounts WHERE id = ANY($1::bigint[]) AND owner_id=$2 "
            "AND is_active AND session_str IS NOT NULL",
            acc_ids, uid,
        )
        acc_ids = [r["id"] for r in (rows or [])]
        if not acc_ids:
            return _err("Нет доступных активных аккаунтов из выбранных", 400)

        # Ban-safety: массовый join/leave — рисковая операция, и бот проверяет
        # давление инфраструктуры ПЕРЕД постановкой. Мини-апп обязан так же,
        # иначе через второй вход тот же аккаунт получит больше нагрузки.
        try:
            from services import infra_orchestrator
            op_name = "bulk_join" if kind == "join" else "bulk_leave"
            ready, reason = await infra_orchestrator.is_ready_for_op(pool, uid, op_name)
            if not ready:
                return _err(f"Инфраструктура перегружена: {reason}", 429)
        except Exception:
            # Гейт давления — защита, а не блокер: его собственный сбой не должен
            # мешать пользователю запустить операцию.
            log.warning("_mass_membership_op(%s): pressure check failed", kind, exc_info=True)

        op_type = "bulk_join" if kind == "join" else "bulk_leave"
        key = "links" if kind == "join" else "channels"
        params = {key: targets, "account_ids": acc_ids, "delay_mode": delay_mode}
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, op_type, params,
                total_items=len(targets) * len(acc_ids),
                label=(f"{'Вступление в' if kind == 'join' else 'Выход из'} "
                       f"{len(targets)} чатов × {len(acc_ids)} акк."),
            )
            return _json_resp({
                "ok": True, "op_id": op_id,
                "targets": len(targets), "accounts": len(acc_ids),
            })
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("_mass_membership_op(%s) uid=%d", kind, uid)
            return _err(_INTERNAL_ERROR, 500)

    async def channels_bulk_post(request: web.Request) -> web.Response:
        """Пост во все (или выбранные) каналы одного аккаунта (паритет с ботом)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)

        acc_id = validate_integer(data.get("acc_id"), min_val=1)
        if not acc_id:
            return _err("Выберите аккаунт", 400)
        text = validate_string(data.get("text"), max_len=4096)
        if not text:
            return _err("Введите текст поста", 400)
        if check_sql_suspicious(text):
            return _err("Недопустимые символы в тексте", 400)

        wanted = [int(x) for x in (data.get("channel_ids") or []) if str(x).lstrip("-").isdigit()]
        rows = await _safe_fetch(
            pool,
            "SELECT channel_id FROM managed_channels WHERE owner_id=$1 AND acc_id=$2"
            + (" AND channel_id = ANY($3::bigint[])" if wanted else ""),
            *([uid, int(acc_id), wanted] if wanted else [uid, int(acc_id)]),
        )
        channel_ids = [int(r["channel_id"]) for r in (rows or [])]
        if not channel_ids:
            return _err("У этого аккаунта нет каналов — сначала импортируйте их", 400)

        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bulk_post_chans",
                {"acc_id": int(acc_id), "channel_ids": channel_ids, "text": text},
                total_items=len(channel_ids),
                label=f"Пост · каналов: {len(channel_ids)} · аккаунт #{acc_id}",
            )
            return _json_resp({"ok": True, "op_id": op_id, "channels": len(channel_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channels_bulk_post uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def channels_reclassify(request: web.Request) -> web.Response:
        """Переопределить «мою инфраструктуру»: живыми сессиями проставить роль
        каждой строке managed_channels и убрать чужие (только участник). Чинит
        уже накопленную засорённость списка чужими подписками."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(
            pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
            "AND session_str IS NOT NULL ORDER BY id",
            uid,
        )
        acc_ids = [int(r["id"]) for r in (rows or [])]
        if not acc_ids:
            return _err("Нет активных аккаунтов с сессией", 400)
        prune = True
        try:
            body = await request.json()
            if isinstance(body, dict) and "prune" in body:
                prune = bool(body["prune"])
        except Exception:
            pass
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "reclassify_channels",
                {"prune": prune},
                total_items=len(acc_ids),
                label="Переопределение моей инфраструктуры",
            )
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channels_reclassify uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def channels_bulk_join(request: web.Request) -> web.Response:
        return await _mass_membership_op(request, "join")

    async def channels_bulk_leave(request: web.Request) -> web.Response:
        return await _mass_membership_op(request, "leave")

    app.router.add_post("/api/miniapp/channels/bulk_create", channels_bulk_create)
    app.router.add_post("/api/miniapp/channels/reclassify", channels_reclassify)
    app.router.add_post("/api/miniapp/channels/bulk_post", channels_bulk_post)
    app.router.add_post("/api/miniapp/channels/bulk_join", channels_bulk_join)
    app.router.add_post("/api/miniapp/channels/bulk_leave", channels_bulk_leave)
    app.router.add_get("/api/miniapp/channels", channels)
    app.router.add_get("/api/miniapp/channel/{ch_id}", channel_detail)
    app.router.add_post("/api/miniapp/channel/{ch_id}/post", post_to_channel)
    app.router.add_post("/api/miniapp/channel/{ch_id}/pin", pin_channel_last_post)
    app.router.add_get("/api/miniapp/channel/{ch_id}/invite_link", channel_invite_link)
    app.router.add_post("/api/miniapp/channel/add", channel_add)
    app.router.add_post("/api/miniapp/channel/{ch_id}/edit", channel_edit)
    app.router.add_post("/api/miniapp/channel/{ch_id}/promote", channel_promote)
    app.router.add_post("/api/miniapp/channels/mass", channels_mass)
    app.router.add_post("/api/miniapp/channels/bulk_delete", channels_bulk_delete)
    app.router.add_delete("/api/miniapp/channel/{ch_id}", channel_remove)
