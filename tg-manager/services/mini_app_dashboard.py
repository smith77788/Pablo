"""Главный экран и «что требует внимания» — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: три маршрута `/api/miniapp/dashboard*`
и три функции — сводка, визуальная карта флота и список того, что требует
внимания оператора.

Дашборд обязан оставаться витриной с кнопками: действия на карточках — это
уже существующие эндпоинты со своими проверками тарифа, предохранителя и
скоупа владельца, а не второй движок массовых операций. Каждый блок
«внимания» обёрнут отдельно, чтобы упавшая выборка гасила свою карточку, а не
весь экран: оператор в аварии не должен получить пустую страницу. Поведение
при переносе не менялось: тела обработчиков и строки регистрации маршрутов
перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты дашборда на приложении мини-аппа."""
    import asyncio

    from services import account_status as _acc_status
    from services.mini_app_api import (
        _cached_user, _dashboard_query, _err, _get_uid, _json_resp,
        _resolve_best_plan, _safe_fetch, _safe_fetchrow, _safe_fetchval, _stats,
        _wants_platform_scope, account_health_summary, log, op_status,
    )

    async def _account_health_summary(uid: int) -> dict:
        """Прежнее имя внутри перенесённых тел — делегат к модульной функции."""
        return await account_health_summary(pool, uid)

    @_cached_user()
    async def dashboard(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)

        # Все запросы дашборда независимы — выполняем их параллельно одним
        # gather вместо ~8 последовательных round-trip'ов к БД.
        async def _plan():
            try:
                from bot.utils.subscription import get_plan as _gp
                return await _gp(pool, uid)
            except Exception:
                return None
        # Скоуп дашборда обязан совпадать с экраном «Аккаунты» (класс #1). Экран
        # теперь owner-scoped по умолчанию (даже у админа) — значит и счётчик/
        # здоровье считаем по СВОИМ. Платформенный срез — только явным
        # ?scope=platform у админа. Иначе два админ-профиля видели одинаковые 25.
        _adm = _wants_platform_scope(request, uid)
        # Здоровье флота — из ЕДИНОГО пульса (infra_memory.get_account_health).
        # Пульс МГНОВЕННО сводит acc_status (бан/спам-блок/заморозка/сессия),
        # активный кулдаун, свежие ограничения и флуды (restriction_events +
        # account_flood_log) и доверие в один балл 0..1 на аккаунт — те же
        # сигналы, по которым операции решают брать аккаунт или нет. Раньше
        # дашборд усреднял ТОЛЬКО trust_score, а он пересчитывается раз в 30 мин
        # (trust_engine): флот во флуде/спам-блоке светил «100%» до следующего
        # цикла. Теперь здоровье = средний балл пульса — падает сразу.
        #
        # Платформенный срез админа (редкий ?scope=platform) пульс не покрывает
        # (он owner-scoped), поэтому там health-aware SQL по всей платформе,
        # считающий ТЕ ЖЕ durable-сигналы: мёртвый статус/нет сессии → 0,
        # активный кулдаун → 35, иначе доверие минус штраф за флуды.
        from services.infra_memory import get_account_health as _get_health
        _health_sql = (
            """SELECT ROUND(AVG(
                    CASE
                        WHEN COALESCE(acc_status,'ok') IN ("""
            + _acc_status.sql_dead_list() + """) THEN 0
                        WHEN session_str IS NULL OR session_str = '' THEN 0
                        WHEN cooldown_until IS NOT NULL AND cooldown_until > now() THEN 35
                        ELSE GREATEST(0, LEAST(100, COALESCE(trust_score, 1.0) * 100)
                                       - 15 * LEAST(4, COALESCE(flood_count_7d, 0)))
                    END
                ))::int FROM tg_accounts WHERE is_active=true"""
        )
        _q = {
            "stats": _stats(pool, uid, _adm),
            "plan": _plan(),
            "plan_row": pool.fetchrow(
                "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1 "
                "AND (plan_expires_at IS NULL OR plan_expires_at > now())", uid),
            "exp_row": pool.fetchrow(
                "SELECT plan, expires_at FROM subscriptions WHERE user_id=$1 AND is_active=true "
                "AND expires_at > now() ORDER BY expires_at DESC LIMIT 1", uid),
            "activity": pool.fetch(
                f"""SELECT COALESCE(label, op_type) AS action, status, created_at,
                          done_items, total_items,
                          {op_status.sql_error_reason()} AS error_msg
                   FROM operation_queue WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 10""", uid),
            "queue_backlog": pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='pending'", uid),
            "ops_failed": pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='failed' AND created_at > NOW() - INTERVAL '24 hours'", uid),
        }
        # Owner — из пульса (мгновенно), платформенный админ — health-aware SQL.
        if _adm:
            _q["acc_health"] = pool.fetchval(_health_sql)
        else:
            _q["pulse"] = _get_health(pool, uid)
        _keys = list(_q.keys())
        _res = await asyncio.gather(
            *(_dashboard_query(name, query) for name, query in _q.items()),
            return_exceptions=True,
        )
        r = {k: (None if isinstance(v, Exception) else v) for k, v in zip(_keys, _res)}

        stats = r["stats"] or {"bots": 0, "channels": 0, "subscribers": 0,
                               "campaigns_active": 0, "funnels_active": 0,
                               "accounts": 0, "ops_running": 0}
        # План. Подписка может жить в РАЗНЫХ источниках (subscriptions — оплата/бот;
        # platform_users.current_plan — ручная выдача админом), а per-process кеш
        # get_plan мог устареть (оплату обработал другой процесс). Раньше логика
        # была `stats["plan"]=get_plan; if not plan: fallback` — но "free" ИСТИННО,
        # поэтому фолбэк на platform_users НЕ срабатывал: Enterprise, выданный в
        # platform_users, показывался как Free. Берём НАИВЫСШИЙ тариф среди всех
        # источников (каждый уже отфильтрован по сроку действия в своём запросе).
        plan_row = r["plan_row"]
        try:
            stats["plan"] = _resolve_best_plan(
                r["plan"],                                          # get_plan (админ → paid)
                (r["exp_row"]["plan"] if r["exp_row"] else None),   # активная подписка
                (plan_row["current_plan"] if plan_row else None),   # ручная выдача (не истёкшая)
            )
            if r["exp_row"]:
                stats["plan_expires_at"] = str(r["exp_row"]["expires_at"])
            elif plan_row and plan_row["plan_expires_at"]:
                stats["plan_expires_at"] = str(plan_row["plan_expires_at"])
            else:
                stats["plan_expires_at"] = None  # бессрочный или free
        except Exception:
            log.exception("dashboard plan resolve uid=%s", uid)
            stats["plan"] = "free"
            stats["plan_expires_at"] = None
        # Лента активности
        def _op_action(s):
            return ("completed" if s == "done" else
                    "running" if s == "running" else
                    "error" if s == "failed" else s)
        try:
            stats["recent_activity"] = [
                {
                    "action": a["action"],
                    "status": _op_action(a["status"]),
                    "created_at": a["created_at"].isoformat() if a["created_at"] else None,
                    "detail": (f'{a["done_items"]}/{a["total_items"]}' if (a["total_items"] or 0) > 0 else None),
                }
                for a in (r["activity"] or [])
            ]
        except Exception:
            stats["recent_activity"] = []
        # Owner: средний балл единого пульса (0..1) → проценты. Платформенный
        # админ: готовое число health-aware SQL. Нет аккаунтов / сбой запроса →
        # 100 (нейтрально, как и было).
        if _adm:
            stats["acc_health"] = int(r["acc_health"]) if r.get("acc_health") is not None else 100
        else:
            _pulse = r.get("pulse") or {}
            _paccs = _pulse.get("accounts") or []
            if _paccs:
                stats["acc_health"] = int(round(
                    sum(float(a.get("score") or 0) for a in _paccs) / len(_paccs) * 100))
            else:
                stats["acc_health"] = 100
        stats["queue_backlog"] = int(r["queue_backlog"] or 0)
        stats["ops_failed"] = int(r["ops_failed"] or 0)
        return _json_resp(stats)

    async def dashboard_visual(request: web.Request) -> web.Response:
        """Данные визуального дашборда (командный центр): аккаунты по статусам,
        здоровье прокси-пула, поток операций за 7 дней, распределение health.
        Один запрос — все метрики (для SVG-графиков на клиенте). Скоуп по owner_id."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        _q = {
            "acc_by_status": _safe_fetch(pool,
                """SELECT COALESCE(acc_status,'active') AS st, COUNT(*) AS c
                   FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE
                   GROUP BY COALESCE(acc_status,'active')""", uid),
            "proxies": _safe_fetchrow(pool,
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE is_alive IS TRUE) AS alive,
                          COUNT(*) FILTER (WHERE is_alive IS FALSE) AS dead,
                          COUNT(*) FILTER (WHERE COALESCE(is_backup,FALSE)) AS backup,
                          COUNT(*) FILTER (WHERE EXISTS (
                              SELECT 1 FROM tg_accounts a
                              WHERE a.owner_id=$1 AND a.proxy_id=user_proxies.id)) AS assigned
                   FROM user_proxies WHERE owner_id=$1""", uid),
            "ops7d": _safe_fetch(pool,
                # 'partial' — терминальное состояние недоведённой работы; без
                # него операция, взявшая часть целей, не попадала НИ В ОДИН
                # счётчик и просто исчезала из графика.
                """SELECT to_char(date_trunc('day', created_at),'MM-DD') AS day,
                          COUNT(*) FILTER (WHERE status='done') AS done,
                          COUNT(*) FILTER (WHERE status='partial') AS partial,
                          COUNT(*) FILTER (WHERE status='failed') AS failed
                   FROM operation_queue
                   WHERE owner_id=$1 AND created_at > now() - INTERVAL '7 days'
                   GROUP BY 1 ORDER BY 1""", uid),
            "ops_now": _safe_fetchrow(pool,
                """SELECT COUNT(*) FILTER (WHERE status='running') AS running,
                          COUNT(*) FILTER (WHERE status='pending') AS pending
                   FROM operation_queue WHERE owner_id=$1""", uid),
            "health": _safe_fetchrow(pool,
                # Здоровье командного центра — те же durable-сигналы, что и
                # единый пульс (мёртвый статус/нет сессии → 0, активный кулдаун
                # → 35, иначе доверие 0..100 минус штраф за флуд). Раньше брали
                # ЧИСТЫЙ trust_score, и флот во флуде/спам-блоке попадал в
                # «хорошие» (good ≥70), пока trust_engine не пересчитает — та же
                # ложь, что чинилась на дашбордном «Здоровье».
                """SELECT ROUND(AVG(h))::int AS avg,
                          COUNT(*) FILTER (WHERE h >= 70) AS good,
                          COUNT(*) FILTER (WHERE h BETWEEN 40 AND 69) AS warn,
                          COUNT(*) FILTER (WHERE h < 40) AS bad
                   FROM (
                     SELECT CASE
                         WHEN COALESCE(acc_status,'ok') IN ("""
                + _acc_status.sql_dead_list() + """) THEN 0
                         WHEN session_str IS NULL OR session_str = '' THEN 0
                         WHEN cooldown_until IS NOT NULL AND cooldown_until > now() THEN 35
                         ELSE GREATEST(0, LEAST(100, COALESCE(trust_score,1.0) * 100)
                                        - 15 * LEAST(4, COALESCE(flood_count_7d,0)))
                     END AS h
                     FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE
                   ) t""", uid),
        }
        _keys = list(_q.keys())
        _res = await asyncio.gather(
            *(_dashboard_query(name, query) for name, query in _q.items()),
            return_exceptions=True,
        )
        r = {k: (None if isinstance(v, Exception) else v) for k, v in zip(_keys, _res)}
        prx = r.get("proxies") or {}
        opn = r.get("ops_now") or {}
        hl = r.get("health") or {}
        return _json_resp({
            "ok": True,
            "accounts": [{"status": row["st"], "count": int(row["c"])}
                         for row in (r.get("acc_by_status") or [])],
            "proxies": {k: int(prx.get(k) or 0)
                        for k in ("total", "alive", "dead", "backup", "assigned")},
            "ops7d": [{"day": row["day"], "done": int(row["done"]), "failed": int(row["failed"])}
                      for row in (r.get("ops7d") or [])],
            "ops_now": {"running": int(opn.get("running") or 0),
                        "pending": int(opn.get("pending") or 0)},
            "health": {"avg": int(hl.get("avg") or 0), "good": int(hl.get("good") or 0),
                       "warn": int(hl.get("warn") or 0), "bad": int(hl.get("bad") or 0)},
        })

    async def dashboard_attention(request: web.Request) -> web.Response:
        """«Что требует внимания» — единственный экран, который отвечает на вопрос
        оператора «что сейчас не так и что с этим сделать».

        Система копит подробную картину своего состояния — упавшие операции,
        мёртвые прокси, карантин аккаунтов, молчащие боты, аккаунты без прокси, —
        но до сих пор всё это лежало по разным экранам или не показывалось вовсе.
        Здесь оно сведено в один список, отсортированный по важности.

        Каждая карточка несёт ДЕЙСТВИЕ, и все действия — уже существующие
        эндпоинты со своими проверками (тариф, предохранитель, скоуп владельца).
        Новых массовых операций тут не заводится: дашборд обязан оставаться
        витриной с кнопками, а не вторым движком.

        Каждый блок обёрнут отдельно: упавшая выборка гасит свою карточку, а не
        весь экран — оператор в аварии не должен получить пустую страницу.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)

        items: list[dict] = []

        def add(kind, sev, title, detail, count, action=None):
            it = {"kind": kind, "severity": sev, "title": title,
                  "detail": detail, "count": int(count)}
            if action:
                it["action"] = action
            items.append(it)

        # ── Операции, которые упали ────────────────────────────────────────
        try:
            rows = await _safe_fetch(pool,
                "SELECT id, op_type, label, "
                f"{op_status.sql_error_reason()} AS last_error, finished_at "
                "FROM operation_queue WHERE owner_id=$1 AND status='failed' "
                "  AND COALESCE(finished_at, created_at) > NOW() - INTERVAL '7 days' "
                "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 5", uid)
            if rows:
                n = int(await _safe_fetchval(pool,
                    "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 "
                    "AND status='failed' AND COALESCE(finished_at, created_at) "
                    "> NOW() - INTERVAL '7 days'", uid) or len(rows))
                first = rows[0]
                add("ops_failed", "high",
                    f"Операций упало за неделю: {n}",
                    (first["label"] or first["op_type"] or "операция")
                    + (f" — {str(first['last_error'])[:90]}" if first["last_error"] else ""),
                    n,
                    {"label": "↻ Повторить последнюю",
                     "endpoint": f"/api/miniapp/operation/{int(first['id'])}/retry",
                     "method": "POST", "reload": True})
        except Exception:
            log.warning("attention: ops_failed", exc_info=True)

        # ── Очередь вообще не разбирается ──────────────────────────────────
        # Отдельная карточка от «застрявших в работе»: там операция идёт слишком
        # долго, здесь её никто не начинал. Пока исполнитель операций не работает,
        # экран показывал ровно «ожидает» — без причины и без срока, то есть
        # владелец не мог отличить нормальную очередь от остановленного продукта.
        # Карточка НАМЕРЕННО без кнопки: изнутри мини-аппа поднять исполнителя
        # нельзя, а «поставить очередь на паузу» здесь было бы прямо во вред.
        try:
            from services import operation_bus as _obus_att

            _frozen_att = await _obus_att.queue_frozen(pool)
            if _frozen_att and int(_frozen_att.get("owners", {}).get(uid, 0)) > 0:
                _n_att = int(_frozen_att["owners"][uid])
                add("ops_frozen", "high",
                    f"Операции не запускаются: {_n_att}",
                    f"Самая старая ждёт {int(_frozen_att.get('oldest_min') or 0)} мин, "
                    "а исполнитель операций не отвечает. Очередь не потеряна — "
                    "операции стартуют сами, как только он заработает.",
                    _n_att)
        except Exception:
            log.warning("attention: ops_frozen", exc_info=True)

        # ── Операции, застрявшие в работе ──────────────────────────────────
        # Порог — СОБСТВЕННЫЙ потолок прогона операции (op_worker.stuck_after_s),
        # а не плоские два часа, и выполняющиеся прямо сейчас операции
        # исключаются. Плоский порог был вдвое-втрое меньше разрешённого прогона
        # (потолок 6 часов), поэтому здоровый массовый инвайт с пейсингом на часы
        # поднимал эту карточку — да ещё с кнопкой «поставить очередь на паузу»,
        # то есть владельца прямо подталкивали остановить работающую операцию.
        try:
            import datetime as _dt_att

            from services import op_worker as _ow_att

            try:
                _active_att = await _ow_att.active_op_ids()
            except Exception:
                _active_att = frozenset()
            _rows_att = await _safe_fetch(pool,
                "SELECT id, op_type, started_at FROM operation_queue "
                "WHERE owner_id=$1 AND status='running' AND started_at IS NOT NULL",
                uid) or []
            _now_att = _dt_att.datetime.now(_dt_att.timezone.utc)
            n = 0
            for _r_att in _rows_att:
                if int(_r_att["id"]) in _active_att:
                    continue
                _st_att = _r_att["started_at"]
                if _st_att.tzinfo is None:
                    _st_att = _st_att.replace(tzinfo=_dt_att.timezone.utc)
                if (_now_att - _st_att).total_seconds() >= _ow_att.stuck_after_s(
                        _r_att["op_type"]):
                    n += 1
            if n:
                add("ops_stuck", "high",
                    f"Операций работает дольше отведённого времени: {n}",
                    "Обычно это оборванная сессия или недоступный Telegram. "
                    "Пауза остановит очередь, не теряя прогресс.",
                    n,
                    {"label": "⏸ Поставить очередь на паузу",
                     "endpoint": "/api/miniapp/operations/pause",
                     "method": "POST", "reload": True})
        except Exception:
            log.warning("attention: ops_stuck", exc_info=True)

        # ── Мёртвые прокси ─────────────────────────────────────────────────
        try:
            row = await _safe_fetchrow(pool,
                "SELECT COUNT(*) AS dead, "
                "  (SELECT COUNT(*) FROM tg_accounts a JOIN user_proxies p ON p.id=a.proxy_id "
                "   WHERE a.owner_id=$1 AND a.is_active AND p.is_alive=FALSE) AS stranded "
                "FROM user_proxies WHERE owner_id=$1 AND is_alive=FALSE", uid)
            dead = int(row["dead"]) if row else 0
            stranded = int(row["stranded"]) if row else 0
            if dead:
                add("proxy_dead", "high" if stranded else "medium",
                    f"Мёртвых прокси: {dead}",
                    (f"На них сидит аккаунтов: {stranded}. Пока прокси мёртв, "
                     "аккаунт не работает и выглядит сломанным."
                     if stranded else "Аккаунтов на них нет — можно просто убрать."),
                    dead,
                    {"label": "🚚 Переселить аккаунты" if stranded else "🧹 Убрать мёртвые",
                     "endpoint": "/api/miniapp/proxy/evacuate" if stranded
                                 else "/api/miniapp/proxy/cleanup_dead",
                     "method": "POST", "reload": True})
        except Exception:
            log.warning("attention: proxy_dead", exc_info=True)

        # ── Активные аккаунты вообще без прокси ────────────────────────────
        try:
            n = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active "
                "AND proxy_id IS NULL", uid) or 0)
            if n:
                add("acc_no_proxy", "medium",
                    f"Аккаунтов без прокси: {n}",
                    "Они выходят с общего адреса сервера. Для флота это самый "
                    "заметный признак связки аккаунтов между собой.",
                    n,
                    {"label": "🔀 Раздать прокси",
                     "endpoint": "/api/miniapp/proxy/rotate",
                     "method": "POST", "reload": True})
        except Exception:
            log.warning("attention: acc_no_proxy", exc_info=True)

        # ── Карантин по единому пульсу здоровья ────────────────────────────
        try:
            summ = await _account_health_summary(uid)
            q = int(summ.get("quarantine") or 0)
            risk = int(summ.get("at_risk") or 0)
            if q or risk:
                add("acc_health", "high" if q else "medium",
                    f"В карантине: {q} · под риском: {risk}",
                    "Пульс сводит ограничения, флуд и доверие в один сигнал. "
                    "Операции такие аккаунты пропускают.",
                    q + risk,
                    {"label": "❤️ Открыть здоровье", "screen": "health"})
        except Exception:
            log.warning("attention: acc_health", exc_info=True)

        # ── Молчащие боты (schema_v198) ────────────────────────────────────
        try:
            n = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 "
                "AND COALESCE(fail_streak,0) > 0", uid) or 0)
            if n:
                add("bots_silent", "high",
                    f"Ботов не отвечают: {n}",
                    "Подписчики пишут, бот молчит. Чаще всего отозван токен — "
                    "его можно заменить, не теряя аудиторию.",
                    n,
                    {"label": "🤖 Открыть ботов", "screen": "bots"})
        except Exception:
            log.warning("attention: bots_silent", exc_info=True)

        # ── Аккаунты на восстановлении ─────────────────────────────────────
        try:
            n = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM account_rehab_state WHERE owner_id=$1 "
                "AND phase IN ('appeal','warming','recheck')", uid) or 0)
            if n:
                add("acc_rehab", "info",
                    f"Восстанавливается аккаунтов: {n}",
                    "Апелляция, прогрев и перепроверка идут сами. "
                    "Вмешательство не нужно — это статус, а не поломка.",
                    n)
        except Exception:
            log.warning("attention: acc_rehab", exc_info=True)

        # ── Каналы без числа участников ────────────────────────────────────
        try:
            n = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 "
                "AND COALESCE(members_count,0) = 0", uid) or 0)
            if n:
                add("ch_stale", "info",
                    f"Каналов без данных: {n}",
                    "Имя, ссылка и число участников подтягиваются из Telegram. "
                    "Если канал меняли не через Infragram — обновите список.",
                    n,
                    {"label": "🔄 Обновить каналы",
                     "endpoint": "/api/miniapp/channels/reclassify",
                     "method": "POST", "body": {"prune": False}, "reload": True})
        except Exception:
            log.warning("attention: ch_stale", exc_info=True)

        _order = {"high": 0, "medium": 1, "info": 2}
        items.sort(key=lambda i: (_order.get(i["severity"], 3), -i["count"]))
        return _json_resp({
            "items": items,
            "high": sum(1 for i in items if i["severity"] == "high"),
            "total": len(items),
        })

    app.router.add_get("/api/miniapp/dashboard", dashboard)
    app.router.add_get("/api/miniapp/dashboard/visual", dashboard_visual)
    app.router.add_get("/api/miniapp/dashboard/attention", dashboard_attention)
