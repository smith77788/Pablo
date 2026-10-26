"""Прогрев — маршруты мини-аппа: аккаунты и чаты.

Вынесено из `services/mini_app_api.py`: восемь маршрутов
`/api/miniapp/warmup*` и восемь функций — обзор, создание плана, массовый
старт, журнал, пауза, возобновление, отмена, удаление. Плюс четыре маршрута
`/api/miniapp/chatwarmup/*` — разогрев ЧАТОВ: список сессий, создание,
смена состояния, список пригодных аккаунтов.

Разогрев чатов живёт здесь, а не отдельным модулем, потому что для владельца
это та же работа «сделать аккаунт похожим на живой»: прогрев аккаунта и
беседа в чате — две её половины, и расходиться им нельзя. Обе берут живые
аккаунты через одну дверь `_alive_accounts` (правду о живости знает
`mini_app_api.alive_accounts`: пульс, карантин, сессия), и обе ограничены
сверху — прогрев планом, беседа `_CW_FLEET_CAP`.

Прогрев — это долгая мягкая активность, которая делает аккаунт похожим на
живой ПЕРЕД массовыми действиями; сам он живёт в `services/warmup_*` и
исполняется через `services/operation_bus.py`. Здесь только разбор запроса,
скоуп владельца и русские ошибки. Поведение при переносе не менялось: тела
обработчиков и строки регистрации маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты прогрева на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _obus, _safe_fetch,
        _safe_fetchrow, _warmup_bulk_core, alive_accounts, log,
    )
    from services import account_status as _acc_status
    from services.security import (
        check_sql_suspicious, validate_integer, validate_string,
    )

    # Сколько аккаунтов максимум греют один чат: выше этого числа разогрев
    # перестаёт быть похожим на живую беседу.
    _CW_FLEET_CAP = 30

    async def _alive_accounts(uid: int, wanted: list[int] | None = None) -> list[int]:
        """Живые аккаунты владельца — тонкая обёртка над общей функцией.

        Правду о «живости» знает mini_app_api.alive_accounts: пульс здоровья,
        карантин, наличие сессии. Здесь только удобная подпись, как было в
        монолите, откуда разогрев чатов переехал.
        """
        return await alive_accounts(pool, uid, wanted)

    async def warmup_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Колонки причин добавляются inline-миграцией на старте. Если она
            # почему-то не прошла, экран прогрева не должен исчезать целиком:
            # откатываемся на старый набор полей — статус будет беднее, но
            # список планов человек увидит. История с cf_relay_url показала,
            # чем заканчивается жёсткая зависимость от свежей колонки.
            _PLAN_COLS = """wp.id, wp.account_id, wp.plan_type, wp.current_day,
                          wp.target_days, wp.daily_actions, wp.status,
                          wp.started_at, wp.last_action_at,
                          ta.phone, ta.first_name, ta.username"""
            _PLAN_SQL = """SELECT {cols}
                   FROM account_warmup_plans wp
                   JOIN tg_accounts ta ON ta.id = wp.account_id
                   WHERE wp.owner_id=$1
                   ORDER BY wp.started_at DESC LIMIT 50"""
            try:
                plans = await pool.fetch(
                    _PLAN_SQL.format(cols=_PLAN_COLS + """,
                          wp.pause_reason, wp.pause_detail,
                          wp.last_skip_reason, wp.last_skip_at"""),
                    uid,
                )
            except Exception:
                log.warning("warmup_overview uid=%d: нет колонок причин, "
                            "показываем план без диагностики", uid)
                plans = await pool.fetch(_PLAN_SQL.format(cols=_PLAN_COLS), uid)
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM account_warmup_plans WHERE owner_id=$1", uid
            )
            active = await pool.fetchval(
                "SELECT COUNT(*) FROM account_warmup_plans WHERE owner_id=$1 AND status='active'", uid
            )
            # Честное состояние каждого плана. Без этого «🟢 Активен · День 3/21»
            # показывался и на плане, который стоит третью неделю, потому что
            # аккаунт забанен, — и человек не имел ни одного способа это увидеть.
            from services import warmup_status as _ws

            out = []
            attention = 0
            for p in plans:
                d = dict(p)
                st = _ws.classify(d)
                for _k in ("started_at", "last_action_at", "last_skip_at"):
                    d[_k] = d[_k].isoformat() if d.get(_k) else None
                d["health"] = st
                if st["attention"]:
                    attention += 1
                out.append(d)
            return _json_resp({
                "total": total, "active": active,
                "attention": attention,
                "plans": out,
            })
        except Exception:
            log.exception("warmup_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def warmup_plan_log(request: web.Request) -> web.Response:
        """Журнал действий прогрева по плану.

        `account_warmup_log` пишется с самого первого дня существования
        прогрева и НИ РАЗУ не показывался пользователю. Из-за этого на вопрос
        «он вообще греется?» ответить было нечем: экран знал только номер дня,
        который движок мог не двигать неделями. Здесь — что именно делал
        аккаунт, что получилось и на чём споткнулось.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        _BASE = """wp.id, wp.account_id, wp.status, wp.current_day,
                      wp.target_days, wp.last_action_at, wp.started_at"""
        _SQL = ("SELECT {cols} FROM account_warmup_plans wp "
                "WHERE wp.id=$1 AND wp.owner_id=$2")
        plan = await _safe_fetchrow(
            pool,
            _SQL.format(cols=_BASE + """, wp.pause_reason, wp.pause_detail,
                      wp.last_skip_reason, wp.last_skip_at"""),
            plan_id, uid)
        if not plan:
            # Либо плана правда нет, либо колонки диагностики ещё не накатились
            # (см. warmup_overview). Второй случай не должен выглядеть как
            # «план не найден» — журнал ценен и без причин.
            plan = await _safe_fetchrow(pool, _SQL.format(cols=_BASE), plan_id, uid)
        if not plan:
            return _err("План не найден", 404)
        rows = await _safe_fetch(
            pool,
            """SELECT action_type, target, success, error, performed_at
                 FROM account_warmup_log
                WHERE account_id=$1
                ORDER BY performed_at DESC
                LIMIT 60""",
            plan["account_id"])
        # Сводка за неделю — по ней сразу видно, идёт работа или только ошибки.
        agg = await _safe_fetchrow(
            pool,
            """SELECT COUNT(*) FILTER (WHERE success) AS ok,
                      COUNT(*) FILTER (WHERE NOT success) AS fail
                 FROM account_warmup_log
                WHERE account_id=$1 AND performed_at > NOW() - INTERVAL '7 days'""",
            plan["account_id"])
        from services import warmup_status as _ws
        from services import account_warmer as _aw

        pd = dict(plan)
        # Круг каналов аккаунта: сколько в обороте и сколько он нашёл сам.
        # Без этого расширение круга — невидимая механика: владелец не сможет
        # ни подтвердить, что она работает, ни заметить, что она встала.
        _found = await _safe_fetch(
            pool,
            """SELECT channel_ref FROM account_warmup_interests
                WHERE account_id=$1
                ORDER BY COALESCE(last_seen_at, found_at) DESC
                LIMIT 12""",
            pd["account_id"])
        _found_refs = [r["channel_ref"] for r in (_found or [])]
        return _json_resp({
            "ok": True,
            "plan": {
                "id": pd["id"], "status": pd["status"],
                "current_day": pd["current_day"], "target_days": pd["target_days"],
                "health": _ws.classify(pd),
                "last_action_at": pd["last_action_at"].isoformat() if pd["last_action_at"] else None,
                "skip_label": _ws.skip_label(pd.get("last_skip_reason")),
            },
            "circle": {
                "size": _aw.channel_pool_size(pd["current_day"]),
                "found": len(_found_refs),
                "examples": _found_refs[:6],
            },
            "week": {"ok": int(agg["ok"] or 0) if agg else 0,
                     "fail": int(agg["fail"] or 0) if agg else 0},
            "actions": [
                {"action": r["action_type"], "target": r["target"],
                 "success": bool(r["success"]), "error": r["error"],
                 "at": r["performed_at"].isoformat() if r["performed_at"] else None}
                for r in rows
            ],
        })

    async def warmup_create_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        plan_type = body.get("plan_type", "standard")
        if plan_type not in ("gentle", "standard", "aggressive"):
            return _err("plan_type должен быть gentle/standard/aggressive", 400)
        if not body.get("account_id"):
            return _err("account_id обязателен", 400)
        # Нечисловой id уезжал параметром в запрос и давал DataError → 500.
        account_id = validate_integer(body.get("account_id"), min_val=1)
        if not account_id:
            return _err("Неверный идентификатор аккаунта", 400)

        # ── Расширенные настройки поведения прогрева (движок читает их из
        # account_niche_profiles) — раньше не выводились в UI, аккаунт всегда
        # грелся как mixed/general. Теперь пользователь задаёт характер. ──
        from services.account_warmer import (
            WARMUP_PROFILES, WARMUP_NICHES, normalize_warmup_channels,
        )
        profile_type = (body.get("profile_type") or "mixed").strip().lower()
        if profile_type not in WARMUP_PROFILES:
            return _err(f"profile_type должен быть одним из: {', '.join(WARMUP_PROFILES)}", 400)
        niche = (body.get("niche") or "general").strip().lower()
        if niche not in WARMUP_NICHES:
            return _err(f"niche должен быть одним из: {', '.join(WARMUP_NICHES)}", 400)
        custom_channels = normalize_warmup_channels(body.get("custom_channels"))

        try:
            acc = await pool.fetchrow(
                "SELECT id, session_str, acc_status, is_active "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2", account_id, uid
            )
        except Exception:
            log.exception("warmup_create_plan fetchrow uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not acc:
            return _err("Аккаунт не найден", 404)
        if not acc["session_str"]:
            return _err("Аккаунт не имеет активной сессии — сначала добавьте .session файл", 400)
        # Пригодность аккаунта проверялась ТОЛЬКО в браузере (список в модалке
        # фильтровался по status==='active'). Любой прямой запрос — или
        # устаревший список на экране — заводил план на забаненном аккаунте:
        # план создавался, показывал «День 0/21» и не двигался никогда.
        from services import warmup_status as _ws

        _ok, _why = _ws.can_resume(None, acc["acc_status"], acc["is_active"])
        if not _ok:
            return _err(_why, 409)
        try:
            days_map = {"gentle": 21, "standard": 14, "aggressive": 10}
            actions_map = {"gentle": 5, "standard": 10, "aggressive": 12}
            target_days = days_map.get(plan_type, 14)
            daily_actions = actions_map.get(plan_type, 10)
            # Опциональные переопределения (продвинутый режим). Клампим по
            # безопасности: >20 действий/день на свежем аккаунте — топ-триггер
            # бана, поэтому жёсткий потолок независимо от ввода пользователя.
            if body.get("daily_actions") is not None:
                try:
                    daily_actions = max(3, min(20, int(body["daily_actions"])))
                except (TypeError, ValueError):
                    return _err("daily_actions должен быть числом 3..20", 400)
            if body.get("target_days") is not None:
                try:
                    target_days = max(5, min(45, int(body["target_days"])))
                except (TypeError, ValueError):
                    return _err("target_days должен быть числом 5..45", 400)

            # Сохраняем поведенческий профиль — движок прогрева читает именно
            # отсюда (profile_type → веса действий, niche/custom_channels → на
            # каких каналах греть).
            await pool.execute(
                """CREATE TABLE IF NOT EXISTS account_niche_profiles (
                       account_id      BIGINT PRIMARY KEY,
                       owner_id        BIGINT NOT NULL,
                       niche           TEXT NOT NULL DEFAULT 'general',
                       profile_type    TEXT NOT NULL DEFAULT 'reader',
                       custom_channels TEXT[] DEFAULT '{}',
                       flood_wait_count INT NOT NULL DEFAULT 0,
                       last_flood_at   TIMESTAMPTZ,
                       updated_at      TIMESTAMPTZ DEFAULT NOW()
                   )"""
            )
            await pool.execute(
                """INSERT INTO account_niche_profiles(account_id, owner_id, niche, profile_type, custom_channels, updated_at)
                   VALUES($1,$2,$3,$4,$5,now())
                   ON CONFLICT (account_id) DO UPDATE
                   SET niche=$3, profile_type=$4, custom_channels=$5, updated_at=now()""",
                account_id, uid, niche, profile_type, custom_channels,
            )

            label = f"Прогрев аккаунта ({plan_type}, {profile_type})"
            op_id = await _obus.submit(
                pool, uid, "account_warmup", {
                    "account_id": account_id, "plan_type": plan_type,
                    "profile_type": profile_type, "niche": niche,
                }, total_items=1, label=label)
            await pool.execute(
                """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (account_id) DO UPDATE
                   SET plan_type=$3, target_days=$4, daily_actions=$5, status='active',
                       started_at=now(),
                       -- Перезапуск снимает старую жалобу: иначе новый план
                       -- показывал бы причину, по которой встал предыдущий.
                       pause_reason=NULL, pause_detail=NULL, paused_at=NULL,
                       pause_notified_at=NULL, last_skip_reason=NULL, last_skip_at=NULL""",
                uid, account_id, plan_type, target_days, daily_actions,
            )
            return _json_resp({
                "ok": True, "op_id": op_id, "label": label,
                "profile_type": profile_type, "niche": niche,
                "custom_channels": len(custom_channels),
                "daily_actions": daily_actions, "target_days": target_days,
            })
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("warmup_create_plan uid=%d acc=%s", uid, account_id)
            return _err(_INTERNAL_ERROR, 500)

    async def warmup_bulk_start(request: web.Request) -> web.Response:
        """Массовый прогрев: создать планы для ВСЕХ подходящих аккаунтов сразу
        (активные, с сессией, ещё без активного плана). Раньше прогрев запускался
        строго по одному аккаунту — масс-действие отсутствовало. Фоновый
        run_warmup_loop подхватит новые active-планы, отдельные op не нужны."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            result = await _warmup_bulk_core(
                pool, uid,
                plan_type=body.get("plan_type") or "standard",
                profile_type=body.get("profile_type") or "mixed",
                niche=body.get("niche") or "general",
            )
            return _json_resp(result)
        except Exception:
            log.exception("warmup_bulk_start uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def warmup_delete_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        try:
            await pool.execute(
                "DELETE FROM account_warmup_plans WHERE id=$1 AND owner_id=$2", plan_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("warmup_delete_plan uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    async def warmup_pause_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        try:
            # pause_reason='user' + метка «уже сообщили»: о своей же паузе
            # уведомлять человека незачем, а причина отличает её от паузы,
            # которую движок ставит при бане или спам-блоке.
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans
                      SET status='paused', pause_reason='user', pause_detail=NULL,
                          paused_at=NOW(), pause_notified_at=NOW()
                   WHERE id=$1 AND owner_id=$2 AND status='active'
                   RETURNING id""",
                plan_id, uid)
            if not row:
                return _err("Активный план не найден", 404)
            return _json_resp({"ok": True, "status": "paused"})
        except Exception:
            log.exception("warmup_pause_plan uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    async def warmup_resume_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        try:
            # Возобновление — не нейтральное действие: на забаненном или
            # флагнутом аккаунте оно снова гонит действия и добивает его.
            # Раньше пауза движка выглядела как обычная, и кнопка «▶» делала
            # ровно это. Проверяем ДО записи.
            pre = await pool.fetchrow(
                """SELECT wp.pause_reason, a.acc_status, a.is_active
                     FROM account_warmup_plans wp
                     JOIN tg_accounts a ON a.id = wp.account_id
                    WHERE wp.id=$1 AND wp.owner_id=$2 AND wp.status='paused'""",
                plan_id, uid)
            if not pre:
                return _err("Приостановленный план не найден", 404)
            from services import warmup_status as _ws

            ok, why = _ws.can_resume(
                pre["pause_reason"], pre["acc_status"], pre["is_active"])
            if not ok:
                return _err(why, 409)
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans
                      SET status='active', pause_reason=NULL, pause_detail=NULL,
                          paused_at=NULL, pause_notified_at=NULL,
                          last_skip_reason=NULL, last_skip_at=NULL
                   WHERE id=$1 AND owner_id=$2 AND status='paused'
                   RETURNING id, account_id""",
                plan_id, uid)
            if not row:
                return _err("Приостановленный план не найден", 404)
            try:
                await pool.execute(
                    """UPDATE tg_accounts SET acc_status='warming'
                       WHERE id=$1 AND is_active=TRUE
                         AND COALESCE(acc_status,'active')='active'""",
                    row["account_id"])
            except Exception:
                log.warning("warmup_resume_plan: acc_status update failed plan=%d", plan_id)
            return _json_resp({"ok": True, "status": "active"})
        except Exception:
            log.exception("warmup_resume_plan uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    async def warmup_cancel_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор плана", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans
                   SET status='cancelled', completed_at=NOW()
                   WHERE id=$1 AND owner_id=$2
                     AND status IN ('active','paused')
                   RETURNING id, account_id""",
                plan_id, uid)
            if not row:
                return _err("Активный план не найден", 404)
            try:
                await pool.execute(
                    """UPDATE tg_accounts SET acc_status='active'
                       WHERE id=$1 AND COALESCE(acc_status,'active')='warming'""",
                    row["account_id"])
            except Exception:
                log.warning("warmup_cancel_plan: acc_status reset failed plan=%d", plan_id)
            return _json_resp({"ok": True, "status": "cancelled"})
        except Exception:
            log.exception("warmup_cancel_plan uid=%d plan=%d", uid, plan_id)
            return _err(_INTERNAL_ERROR, 500)

    async def chatwarmup_sessions(request: web.Request) -> web.Response:
        """Список сессий разогрева чатов владельца."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import chat_warmup
            return _json_resp({"ok": True, "sessions": await chat_warmup.list_sessions(pool, uid)})
        except Exception:
            log.exception("chatwarmup_sessions uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def chatwarmup_create(request: web.Request) -> web.Response:
        """Создать сессию разогрева: {chat_ref, account_ids[], mode, topics, intensity}.
        Флот вступит в чат и начнёт осмысленный диалог (фоновый цикл chat_warmup)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        chat_ref = validate_string(body.get("chat_ref"), max_len=256)
        if not chat_ref:
            return _err("Укажите чат (@username, ссылка или ID)", 400)
        if check_sql_suspicious(chat_ref):
            return _err("Недопустимые символы в ссылке на чат", 400)
        ids = [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()]
        acc_ids = await _alive_accounts(uid, ids or None)
        if not acc_ids:
            return _err("Выберите хотя бы один живой аккаунт для разогрева", 400)
        # Потолок флота на один чат был молчаливым: отметив 50 аккаунтов,
        # владелец получал разогрев тридцатью и ни слова об остальных.
        requested_accs = len(acc_ids)
        acc_ids = acc_ids[:_CW_FLEET_CAP]
        from services import chat_warmup
        mode = chat_warmup.valid_mode(body.get("mode"))
        topics = validate_string(body.get("topics"), max_len=500) or ""
        intensity = (body.get("intensity") or "normal")
        if intensity not in ("calm", "normal", "active"):
            intensity = "normal"
        try:
            s = await chat_warmup.create_session(pool, uid, chat_ref, acc_ids,
                                                 mode=mode, topics=topics, intensity=intensity)
            return _json_resp({"ok": True, "session": s, "accounts": len(acc_ids),
                               "requested": requested_accs,
                               "omitted": requested_accs - len(acc_ids)})
        except Exception:
            log.exception("chatwarmup_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def chatwarmup_status(request: web.Request) -> web.Response:
        """Сменить статус сессии: {status: active|paused|stopped}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sid = int(request.match_info["sid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        status = body.get("status")
        from services import chat_warmup
        ok = await chat_warmup.set_status(pool, uid, sid, status)
        return _json_resp({"ok": ok}) if ok else _err("Сессия не найдена или неверный статус", 404)

    async def chatwarmup_detail(request: web.Request) -> web.Response:
        """Ход одной сессии разогрева: сводка, вклад каждого аккаунта и журнал.

        До этого у разогрева снаружи был ровно один показатель — «отправлено N».
        Строку в списке нельзя было даже открыть, поэтому вопрос «почему флот
        молчит» не имел ответа в принципе: причин у молчаливого хода десяток
        (аккаунт на кулдауне, в карантине, занят операцией, флот не вошёл в чат,
        ночное окно, генератор ответов недоступен, пауза Telegram), и ни одна
        никуда не попадала. Теперь каждый ход пишется в chat_warmup_log, а этот
        экран его показывает.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sid = int(request.match_info["sid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        from services import chat_warmup

        sess = await chat_warmup.get_session(pool, uid, sid)
        if not sess:
            return _err("Сессия не найдена", 404)

        # Вклад каждого аккаунта: сколько написал и сколько ходов не состоялось.
        # Подпись берём из флота — владелец узнаёт аккаунт по имени, не по id.
        by_acc = await _safe_fetch(pool,
            """SELECT l.account_id,
                      COALESCE(a.username, a.first_name, a.phone, '#' || l.account_id::text) AS label,
                      COUNT(*) FILTER (WHERE l.status='ok') AS sent,
                      SUM(CASE WHEN l.status<>'ok' THEN l.repeats ELSE 0 END) AS missed,
                      MAX(l.created_at) AS last_at
                 FROM chat_warmup_log l
                 LEFT JOIN tg_accounts a ON a.id = l.account_id
                WHERE l.session_id=$1 AND l.owner_id=$2 AND l.account_id IS NOT NULL
                GROUP BY l.account_id, a.username, a.first_name, a.phone
                ORDER BY sent DESC, missed DESC""", sid, uid) or []

        rows = await _safe_fetch(pool,
            """SELECT l.id, l.account_id, l.kind, l.status, l.reason, l.text,
                      l.reply_to, l.repeats, l.created_at,
                      COALESCE(a.username, a.first_name, a.phone, '#' || l.account_id::text) AS label
                 FROM chat_warmup_log l
                 LEFT JOIN tg_accounts a ON a.id = l.account_id
                WHERE l.session_id=$1 AND l.owner_id=$2
                ORDER BY l.created_at DESC, l.id DESC LIMIT 60""", sid, uid) or []

        def _iso(v):
            try:
                return v.isoformat()
            except Exception:
                return None

        # Подсказка на случай пустого журнала: сразу после обновления записей
        # ещё нет, а вопрос «почему флот молчит» уже есть. Самая частая причина
        # отвечается без единого хода — писать нечем, ключа ИИ нет.
        hint = ""
        if not any(r["status"] == "ok" for r in rows):
            try:
                from services import ai_claude
                from services.ai_providers import configured_providers

                if not (ai_claude.enabled() or configured_providers()):
                    hint = ("Не задан ни один ключ ИИ: флот вступает в чат, но "
                            "писать ему нечем. Задайте ключ (Claude или Groq) в "
                            "админ-меню «Ключи ИИ».")
            except Exception:
                log.debug("chatwarmup_detail: статус ИИ недоступен", exc_info=True)

        # Живая проверка тех же гейтов, что отказывают ходу: ответ нужен в ту
        # секунду, когда владелец открыл экран, а не после следующего хода.
        diag = await chat_warmup.diagnose(pool, uid, sess)

        return _json_resp({
            "ok": True,
            "hint": hint,
            "diagnose": diag,
            "session": {
                **{k: v for k, v in dict(sess).items()
                   if k in ("id", "chat_ref", "mode", "topics", "intensity",
                            "status", "messages_sent")},
                "accounts": len(sess.get("account_ids") or []),
                "last_run_at": _iso(sess.get("last_run_at")),
                "created_at": _iso(sess.get("created_at")),
            },
            "by_account": [
                {"account_id": r["account_id"], "label": r["label"],
                 "sent": int(r["sent"] or 0), "missed": int(r["missed"] or 0),
                 "last_at": _iso(r["last_at"])}
                for r in by_acc
            ],
            "log": [
                {"id": r["id"], "account_id": r["account_id"], "label": r["label"],
                 "kind": r["kind"], "status": r["status"], "reason": r["reason"],
                 "text": r["text"], "reply_to": r["reply_to"],
                 "repeats": int(r["repeats"] or 1), "at": _iso(r["created_at"])}
                for r in rows
            ],
        })

    async def chatwarmup_accounts(request: web.Request) -> web.Response:
        """Живой флот владельца для выбора под разогрев чата (id + подпись)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            "SELECT id, phone, first_name, username FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active AND session_str IS NOT NULL "
            "AND " + _acc_status.sql_not_dead() + " "
            "ORDER BY trust_score DESC NULLS LAST, added_at LIMIT 200", uid)
        accs = [{"id": r["id"],
                 "label": (r["username"] or r["first_name"] or r["phone"] or f"#{r['id']}")}
                for r in (rows or [])]
        return _json_resp({"ok": True, "accounts": accs})

    app.router.add_get("/api/miniapp/warmup", warmup_overview)
    app.router.add_post("/api/miniapp/warmup", warmup_create_plan)
    app.router.add_post("/api/miniapp/warmup/bulk_start", warmup_bulk_start)
    app.router.add_get("/api/miniapp/warmup/{plan_id}/log", warmup_plan_log)
    app.router.add_delete("/api/miniapp/warmup/{plan_id}", warmup_delete_plan)
    app.router.add_post("/api/miniapp/warmup/{plan_id}/pause", warmup_pause_plan)
    app.router.add_post("/api/miniapp/warmup/{plan_id}/resume", warmup_resume_plan)
    app.router.add_post("/api/miniapp/warmup/{plan_id}/cancel", warmup_cancel_plan)
    app.router.add_get("/api/miniapp/chatwarmup/sessions", chatwarmup_sessions)
    app.router.add_post("/api/miniapp/chatwarmup/session", chatwarmup_create)
    app.router.add_post("/api/miniapp/chatwarmup/session/{sid}/status", chatwarmup_status)
    app.router.add_get("/api/miniapp/chatwarmup/session/{sid}", chatwarmup_detail)
    app.router.add_get("/api/miniapp/chatwarmup/accounts", chatwarmup_accounts)
