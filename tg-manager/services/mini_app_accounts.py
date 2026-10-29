"""Аккаунты Telegram владельца — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 39 маршрутов `/api/miniapp/account*`
и `/api/miniapp/accounts*` — список и карточка, массовые действия, профиль,
диалоги консоли, прокси и заметки, жалоба на спам-блок, оживление, прогрев,
здоровье, и четыре способа добавить аккаунт (телефон, QR, файл сессии, архив
tdata).

Вместе с ручками уехали семь замыканий, которыми пользуется ТОЛЬКО этот блок:
`_accounts_where`, `_build_profile_params`, `_console_account`,
`_multipart_file_and_proxy`, `_parse_access_hash`, `_persist_login`,
`_resolve_proxy_url`. Условие готовности аккаунта (`ACTIVE_ACC_SQL`) осталось
в монолите на уровне модуля: его читает не только этот блок, и второй копии у
него быть не должно — именно из-за расхождения счётчик «готовых» когда-то
считал мёртвые аккаунты.

В монолите остался `accounts_bulk_profile`: двухстрочная обёртка над ОБЩИМ
замыканием `_bulk_edit_op`, которым пользуются ещё боты и каналы.

Аккаунт — главный расходуемый ресурс продукта: его теряют за бан, поэтому
темп, карантин и выбор под нагрузкой живут не здесь, а в
`services/resource_selector.py`, `services/infra_memory.py` и
`services/account_manager.py`. Здесь только разбор запроса, скоуп владельца,
русские ошибки и вызов движка. Поведение при переносе не менялось: тела
обработчиков и строки регистрации маршрутов перенесены дословно.
"""
from __future__ import annotations

import asyncio
import re

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты аккаунтов на приложении мини-аппа."""
    from services.mini_app_api import (
        ACCOUNT_FILTERS, ACCOUNT_STAGES, ACTIVE_ACC_SQL, _INTERNAL_ERROR,
        _body_wants_platform_scope, _csv_resp, _err, _geo_code, _get_uid,
        _json_resp, _obus, _proxy_display_host, _safe_count, _safe_fetch,
        _safe_fetchrow, _safe_fetchval, _text, _wants_platform_scope, log,
        normalize_phone, read_upload, read_upload_text,
    )
    from services.security import validate_string

    def _accounts_where(uid: int, flt: str, stage: str, q: str, admin: bool = False,
                        acc_pool: str = "", geo: str = "") -> tuple[str, list]:
        """Собрать WHERE + args для списка аккаунтов из фильтра здоровья, CRM-статуса
        и поиска. Возвращает (sql_where, args). Серверная фильтрация снимает
        100-лимит клиента: срез считается по ВСЕЙ таблице, не по загруженной странице.
        admin=True — межтенантный просмотр (все аккаунты платформы, без owner-скоупа)."""
        if admin:
            clauses = ["TRUE"]
            args: list = []
        else:
            clauses = ["owner_id=$1"]
            args = [uid]
        if flt == "active":
            # Честный «активный» = тот, кого реально возьмёт операция. Список и
            # счётчик обязаны показывать ОДИН набор: клик по чипу «Активные N»
            # открывает ровно тех, кого обещал счётчик, и масс-операция по этому
            # срезу идёт ровно по ним. Условие — общее (ACTIVE_ACC_SQL).
            clauses.append(ACTIVE_ACC_SQL)
        elif flt == "cooldown":
            clauses.append("cooldown_until IS NOT NULL AND cooldown_until > now()")
        elif flt == "banned":
            clauses.append("COALESCE(acc_status,'ok') = 'banned'")
        elif flt == "spamblock":
            clauses.append("COALESCE(acc_status,'ok') = 'spamblock'")
        elif flt == "proxy_down":
            # Аккаунты, простаивающие ИЗ-ЗА ПРОКСИ, а не по своей вине: сами они
            # целы, но каждая их операция падает по сети. Без этого среза мёртвый
            # прокси нельзя было ни увидеть списком, ни починить оптом.
            # Строго is_alive=FALSE: непроверенный прокси (NULL) — не мёртвый.
            clauses.append(
                "proxy_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM user_proxies p WHERE p.id = tg_accounts.proxy_id "
                "AND p.is_alive IS FALSE)")
        elif flt == "dead":
            # Невоскрешаемые: забанен / удалён-деактивирован / сессия отозвана.
            # Именно их массово удаляют «в один тап». Конфликт двух IP (cooldown)
            # СЮДА не входит — он лечится перезаливом, а не удалением.
            clauses.append("COALESCE(acc_status,'ok') IN ('banned','deactivated','session_expired')")
        if stage in ACCOUNT_STAGES:
            args.append(stage)
            if stage == "new":
                # «Новый» в разбивке включает аккаунты без проставленной стадии
                # (stage IS NULL трактуется как 'new'). Фильтр обязан показывать
                # ровно тех, кого считает чип, иначе клик по «Новый N» открыл бы
                # меньше, чем обещал счётчик.
                clauses.append(f"(stage = ${len(args)} OR stage IS NULL)")
            else:
                clauses.append(f"stage = ${len(args)}")
        if q:
            args.append(f"%{q}%")
            n = len(args)
            clauses.append(f"(phone ILIKE ${n} OR first_name ILIKE ${n} OR username ILIKE ${n})")
        # Пул — то, чем владелец делит флот на группы. На экране «Инфраструктура»
        # он был виден числом («Пул europe — 42 акк») и ничем больше: открыть
        # эти 42 аккаунта было нечем, среза под пул не существовало.
        if acc_pool:
            args.append(acc_pool)
            clauses.append(f"pool = ${len(args)}")
        # Страна аккаунта = страна его прокси (так же считает geo_router). На
        # карте присутствия страна была видна столбиком и числом, и ничем
        # больше: открыть «эти 42 аккаунта в Германии» было нечем.
        if geo:
            args.append(geo)
            clauses.append(
                "COALESCE((SELECT UPPER(p.geo_country) FROM user_proxies p "
                "WHERE p.id = tg_accounts.proxy_id), 'UNKNOWN') = ${}".format(len(args)))
        return " AND ".join(clauses), args

    async def accounts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # По умолчанию — СВОИ аккаунты (даже для админа). Платформенный срез —
        # только явным ?scope=platform. Иначе два админ-профиля видели одинаковые
        # 25 аккаунтов всей платформы вместо своих.
        admin = _wants_platform_scope(request, uid)
        # Параметры серверной пагинации/фильтрации
        qs = request.rel_url.query
        flt = qs.get("filter", "all")
        if flt not in ACCOUNT_FILTERS:
            flt = "all"
        stage = (qs.get("stage") or "").strip().lower()
        if stage and stage not in ACCOUNT_STAGES:
            stage = ""
        q = (qs.get("q") or "").strip()[:64]
        acc_pool = (qs.get("pool") or "").strip()[:64]
        acc_geo = _geo_code(qs.get("geo"))
        try:
            offset = max(0, int(qs.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = min(200, max(1, int(qs.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100

        # admin=True — межтенантный просмотр всех аккаунтов платформы (без owner-скоупа).
        where, args = _accounts_where(uid, flt, stage, q, admin=admin,
                                      acc_pool=acc_pool, geo=acc_geo)
        page_args = args + [limit, offset]
        rows = await _safe_fetch(pool,
            f"""SELECT id, phone, first_name, username, is_active, last_used, added_at,
                      -- trust_score хранится 0..1 → отдаём как проценты 0..100
                      -- (фронт рендерит как «${{trust}}%»). Без *100 всё было ~1%.
                      ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score,
                      COALESCE(acc_status, 'ok') AS acc_status,
                      cooldown_until, cluster, stage,
                      -- Без сессии аккаунт не берёт ни одна операция (условие
                      -- двери выбора). Строке это тоже нужно: иначе экран
                      -- показывает «Активен» там, где работать нечем.
                      (session_str IS NOT NULL) AS has_session,
                      -- Живость прокси СКАЛЯРНЫМ подзапросом, а не JOIN: у
                      -- tg_accounts и user_proxies совпадают id/owner_id/
                      -- is_active, и после JOIN условия из _accounts_where
                      -- стали бы неоднозначными — запрос упал бы, а список
                      -- аккаунтов молча опустел.
                      (proxy_id IS NOT NULL) AS has_proxy,
                      (SELECT p.is_alive FROM user_proxies p
                        WHERE p.id = tg_accounts.proxy_id) AS proxy_alive,
                      (SELECT COALESCE(p.label, p.geo_country) FROM user_proxies p
                        WHERE p.id = tg_accounts.proxy_id) AS proxy_label
               FROM tg_accounts WHERE {where}
               ORDER BY is_active DESC, last_used DESC NULLS LAST, id
               LIMIT ${len(args)+1} OFFSET ${len(args)+2}""", *page_args)
        # Сколько всего под текущим фильтром — для пагинации «Загрузить ещё».
        filtered_total = await _safe_count(pool,
            f"SELECT COUNT(*) FROM tg_accounts WHERE {where}", *args)
        filtered_total = int(filtered_total or 0)

        # Единый риск-пульс поверх сырого acc_status: аккаунт может быть is_active
        # («Активен» в UI), но иммунная система (restriction_events/flood/trust/
        # health_score) держит его в карантине/риске — и операции его тихо
        # пропускают (is_account_quarantined fail-open). Без этого пользователь
        # видит «Активен», а действия по нему не идут («аккаунты не работают,
        # хотя активны»). Мержим health_status в каждую строку (owner-scoped;
        # для admin межтенантного просмотра пропускаем — health по своим).
        rows = [dict(r) for r in (rows or [])]
        if not admin and rows:
            try:
                from services.infra_memory import get_account_health
                _hp = await get_account_health(pool, uid)
                _hmap = {a["account_id"]: a for a in _hp.get("accounts", [])}
                for r in rows:
                    h = _hmap.get(r["id"])
                    if h:
                        r["health_status"] = h["status"]          # quarantine|at_risk|healthy
                        r["health_score"] = h["score"]
                        r["restrictions"] = h.get("restrictions", 0)
                        r["floods"] = h.get("floods", 0)
                        # Причина — рядом со статусом: список показывал «🛑 На
                        # паузе» без неё, и владелец шёл искать объяснение в
                        # карточку аккаунта.
                        r["health_reason"] = h.get("reason") or ""
                        r["health_needs_owner"] = bool(h.get("needs_owner"))
            except Exception as _e:  # fail-soft: пульс не должен ронять список
                log.debug("accounts health merge owner=%s: %s", uid, _e)

        # Одно действие-лечение на строку. Считается ПОСЛЕ мержа пульса —
        # карантин/риск видны только оттуда. Список важнее подсказки: любая
        # ошибка здесь молча оставляет строки без кнопок.
        try:
            from services import account_fix as _afix
            _afix.annotate(rows)
        except Exception as _e:  # pragma: no cover
            log.debug("accounts fix annotate owner=%s: %s", uid, _e)

        # Серверная агрегация KPI (глобальные счётчики, не срез). Админ — по всей
        # платформе, обычный пользователь — по своим аккаунтам.
        if admin:
            st = await _safe_fetchrow(pool,
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok')='banned') AS banned,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok')='spamblock') AS spamblock,
                          COUNT(*) FILTER (WHERE cooldown_until IS NOT NULL AND cooldown_until > now()) AS cooldown,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok') IN ('banned','deactivated','session_expired')) AS dead,
                          COUNT(*) FILTER (WHERE """ + ACTIVE_ACC_SQL + """) AS active
                   FROM tg_accounts""")
        else:
            st = await _safe_fetchrow(pool,
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok')='banned') AS banned,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok')='spamblock') AS spamblock,
                          COUNT(*) FILTER (WHERE cooldown_until IS NOT NULL AND cooldown_until > now()) AS cooldown,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok') IN ('banned','deactivated','session_expired')) AS dead,
                          COUNT(*) FILTER (WHERE """ + ACTIVE_ACC_SQL + """) AS active,
                          -- Простаивают из-за прокси: аккаунт цел, чинить надо
                          -- прокси. Строго IS FALSE — непроверенный не считаем.
                          COUNT(*) FILTER (
                              WHERE proxy_id IS NOT NULL AND EXISTS (
                                  SELECT 1 FROM user_proxies p
                                   WHERE p.id = tg_accounts.proxy_id AND p.is_alive IS FALSE)
                          ) AS proxy_down
                   FROM tg_accounts WHERE owner_id=$1""", uid)
        stats = {k: int((st[k] if st and k in st.keys() else 0) or 0)
                 for k in ("total", "banned", "spamblock", "cooldown", "dead",
                           "active", "proxy_down")} if st else {}
        # by_stage скоупим по owner_id для не-админа (иначе — межтенантная утечка
        # разбивки стадий по ВСЕЙ платформе). Админ видит всё, как в основной статистике.
        # Аккаунты без проставленной CRM-стадии (stage IS NULL) считаем «new» —
        # это и есть первая стадия воронки «только заведён, ещё не обработан».
        # Иначе разбивка охватывала лишь аккаунты со стадией, и чип «Все»
        # показывал их сумму (7 из 52) — владелец читал это как «всего 7
        # аккаунтов». Теперь COALESCE(stage,'new') покрывает ВЕСЬ флот, и сумма
        # чипов сходится с «Все 52» сверху (жалоба: «количество аккаунтов и
        # готовых не верное»).
        if admin:
            stage_rows = await _safe_fetch(pool,
                "SELECT COALESCE(stage,'new') AS stage, COUNT(*) AS c FROM tg_accounts "
                "GROUP BY COALESCE(stage,'new')")
        else:
            stage_rows = await _safe_fetch(pool,
                "SELECT COALESCE(stage,'new') AS stage, COUNT(*) AS c FROM tg_accounts "
                "WHERE owner_id=$1 GROUP BY COALESCE(stage,'new')", uid)
        by_stage = {r["stage"]: int(r["c"] or 0) for r in (stage_rows or [])
                    if r.get("stage") in ACCOUNT_STAGES}
        stats["by_stage"] = by_stage
        return _json_resp({
            "accounts": rows,
            "stats": stats,
            "admin_view": admin,
            "page": {
                "offset": offset, "limit": limit,
                "filtered_total": filtered_total,
                "has_more": offset + len(rows) < filtered_total,
                "filter": flt, "stage": stage, "q": q, "pool": acc_pool,
            },
        })

    async def account_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Платформенный срез только по явному ?scope=platform (по умолчанию — свой).
        admin = _wants_platform_scope(request, uid)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        if admin:
            acc = await _safe_fetchrow(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          status_reason, cooldown_until, cluster, stage, proxy_id
                   FROM tg_accounts WHERE id=$1""", acc_id)
        else:
            acc = await _safe_fetchrow(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          status_reason, cooldown_until, cluster, stage, proxy_id
                   FROM tg_accounts WHERE id=$1 AND owner_id=$2""", acc_id, uid)
        if not acc:
            return _err("Аккаунт не найден", 404)
        caps = await _safe_fetchrow(pool,
            """SELECT can_invite, can_dm, can_create_channel, can_create_bot,
                      can_set_username, is_premium, has_2fa,
                      daily_dm_limit, daily_invite_limit
               FROM account_capabilities WHERE account_id=$1""", acc_id)
        warmup = await _safe_fetchrow(pool,
            """SELECT id, current_day, target_days, status, started_at
               FROM account_warmup_plans WHERE account_id=$1
               ORDER BY started_at DESC LIMIT 1""", acc_id)
        # Состояние восстановления. Без него карточка аккаунта со спам-блоком
        # была тупиком: статус «ограничен» и ни слова о том, что система уже
        # который день вытаскивает этот аккаунт (или давно сдалась и ждёт
        # решения человека).
        rehab = None
        _rh_row = await _safe_fetchrow(pool,
            """SELECT phase, note, attempts, next_action_at
                 FROM account_rehab_state WHERE acc_id=$1""", acc_id)
        if _rh_row:
            try:
                from services import account_rehab as _rh

                rehab = _rh.describe(dict(_rh_row))
            except Exception:
                log.debug("account_detail rehab acc=%s", acc_id)
        recent_ops = await _safe_fetch(pool,
            """SELECT op_type, label, status, done_items, total_items, created_at
               FROM operation_queue WHERE owner_id=$1
               ORDER BY created_at DESC, id DESC LIMIT 8""", uid)
        # Риск-пульс аккаунта — объясняет «активен, но операции его пропускают»
        # (тот же единый сигнал, что и в списке; owner-scoped, fail-soft).
        health = None
        if not admin:
            try:
                from services.infra_memory import get_account_health
                _hp = await get_account_health(pool, uid)
                health = next((a for a in _hp.get("accounts", [])
                               if a["account_id"] == acc_id), None)
            except Exception as _e:
                log.debug("account_detail health owner=%s acc=%s: %s", uid, acc_id, _e)
        # Прокси/IP аккаунта — ЧЕРЕЗ ЧТО он выходит в сеть. Пользователь должен
        # видеть свой реальный IP и уникален ли он. Транспорт (account_manager):
        # свой прокси (уник. IP 1:1) → свой IPv6 → CF-relay (ОБЩИЙ IP) → прямое.
        # Только для владельца: прокси/IPv6 привязаны к owner_id; для admin
        # межтенантного просмотра lookup по его uid дал бы неверный «общий IP».
        transport = None
        pid = None
        if not admin:
            try:
                pid = acc["proxy_id"]
            except Exception:
                pid = None
        if pid:
            prow = await _safe_fetchrow(pool,
                "SELECT id, label, proxy_url, proxy_type, is_alive FROM user_proxies "
                "WHERE id=$1 AND owner_id=$2", pid, uid)
            if prow:
                from services.token_vault import decrypt_token
                host = ""
                try:
                    host = _proxy_display_host(decrypt_token(prow["proxy_url"]) or "")
                except Exception:
                    host = ""
                transport = {
                    "kind": "proxy", "unique": True,
                    "proxy_id": prow["id"],
                    "label": prow["label"] or "",
                    "host": host,                       # это и есть выходной IP/хост аккаунта
                    "proxy_type": prow["proxy_type"] or "socks5",
                    "is_alive": prow["is_alive"],
                    "title": "Свой прокси — уникальный IP 1:1",
                }
        if transport is None and not admin:
            # Нет своего прокси: IPv6 (если настроен у владельца) или общий CF-relay/прямое.
            ipv6 = False
            try:
                from database.db import get_ipv6_subnet
                ipv6 = bool(await get_ipv6_subnet(pool, uid))
            except Exception:
                ipv6 = False
            if ipv6:
                transport = {"kind": "ipv6", "unique": True,
                             "title": "Свой IPv6 — уникальный IP без прокси"}
            else:
                transport = {"kind": "shared", "unique": False,
                             "title": "Без своего прокси — общий CF-relay/прямое (НЕ уникальный IP)"}
        return _json_resp({
            "account": acc,
            "capabilities": caps,
            "warmup": warmup,
            "rehab": rehab,
            "recent_ops": recent_ops,
            "health": health,
            "transport": transport,
        })

    async def accounts_export(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Экспорт — свои аккаунты; вся платформа только по явному ?scope=platform.
        admin = _wants_platform_scope(request, uid)
        if admin:
            rows = await _safe_fetch(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          cooldown_until
                   FROM tg_accounts
                   ORDER BY is_active DESC, last_used DESC NULLS LAST""")
        else:
            rows = await _safe_fetch(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          ROUND(COALESCE(trust_score, 1.0) * 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          cooldown_until
                   FROM tg_accounts WHERE owner_id=$1
                   ORDER BY is_active DESC, last_used DESC NULLS LAST""", uid)
        header = ["id", "phone", "first_name", "username", "tg_user_id",
                  "is_active", "trust_score", "acc_status", "cooldown_until",
                  "last_used", "added_at"]
        csv_rows = []
        for r in rows:
            cd = r.get("cooldown_until")
            csv_rows.append([
                r.get("id"),
                r.get("phone"),
                r.get("first_name"),
                r.get("username"),
                r.get("tg_user_id"),
                r.get("is_active"),
                r.get("trust_score"),
                r.get("acc_status"),
                cd.isoformat() if cd else "",
                r.get("last_used"),
                r.get("added_at"),
            ])
        return _csv_resp("accounts_export.csv", header, csv_rows)

    async def accounts_check(request: web.Request) -> web.Response:
        """Массовая проверка аккаунтов (админ: все, обычный: свои)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # «Проверить все» = ВСЕ аккаунты в области видимости, включая неактивные:
        # смысл проверки — в т.ч. вернуть в строй ошибочно отключённые (об этом и
        # предупреждение в UI). Фильтр is_active здесь выбирал только активные
        # (6 из 25) и противоречил обещанию кнопки. Скоуп: по умолчанию СВОИ;
        # вся платформа — только явным ?scope=platform у админа (не по умолчанию,
        # иначе админ проверял бы чужие аккаунты).
        admin = _wants_platform_scope(request, uid)
        if admin:
            rows = await _safe_fetch(pool, "SELECT id FROM tg_accounts")
        else:
            rows = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1", uid)
        ids = [int(r["id"]) for r in (rows or [])]
        if not ids:
            return _err("Нет аккаунтов для проверки", 400)
        try:
            _params = {"account_ids": ids, "check_spambot": True}
            if admin:
                # Проверка подключается к Telegram сессией аккаунта, поэтому
                # исполнитель по умолчанию чужие id отбрасывает. Право на
                # платформенный срез проверено выше — отмечаем его явно.
                _params["cross_owner"] = True
            op_id = await _obus.submit(
                pool, uid, "check_accounts_health", _params,
                total_items=len(ids), label=f"Проверка · аккаунтов: {len(ids)}",
            )
            return _json_resp({"ok": True, "op_id": op_id, "count": len(ids)})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("accounts_check uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    def _build_profile_params(op: str, body: dict, ids: list) -> tuple:
        """Собрать params для profile_setter. Возвращает (params, label) или (None, error)."""
        params: dict = {"op": op, "account_ids": ids}
        if op == "name":
            fn = (body.get("first_name") or "").strip()
            if not fn:
                return None, "Укажите имя"
            params["name_data"] = {
                "first_name": fn,
                "last_name": (body.get("last_name") or "").strip(),
                "about": (body.get("about") or "").strip(),
            }
            return params, "Смена имени/bio"
        if op == "avatar":
            url = (body.get("avatar_url") or "").strip()
            if not url:
                return None, "Укажите ссылку на аватар"
            params["avatar_url"] = url
            return params, "Смена аватара"
        if op == "2fa":
            np = (body.get("new_password") or "").strip()
            if not np:
                return None, "Укажите новый пароль"
            params["new_password"] = np
            params["current_password"] = (body.get("current_password") or "").strip()
            params["hint"] = (body.get("hint") or "").strip()
            return params, "Смена 2FA"
        if op == "username":
            un = (body.get("username") or "").strip().lstrip("@")
            if not un:
                return None, "Укажите username"
            params["username"] = un
            return params, "Смена username"
        if op == "bio":
            about = (body.get("about") or "").strip()
            if not about:
                return None, "Укажите текст bio"
            params["about"] = about[:70]
            return params, "Смена bio"
        if op == "close_sessions":
            return params, "Закрыть сторонние сессии"
        if op == "privacy":
            key = (body.get("privacy_key") or "").strip()
            if key not in ("phone", "invite", "lastseen"):
                return None, "Неизвестный ключ приватности"
            params["privacy_key"] = key
            params["privacy_allow"] = bool(body.get("privacy_allow"))
            return params, "Настройка приватности"
        if op == "reset_2fa":
            cp = (body.get("current_password") or "").strip()
            if not cp:
                return None, "Укажите текущий пароль 2FA для снятия"
            params["current_password"] = cp
            return params, "Снятие 2FA"
        if op in ("clear_bio", "remove_username", "remove_avatar", "set_online", "check_restriction"):
            return params, {"clear_bio": "Очистка bio", "remove_username": "Снятие username",
                            "remove_avatar": "Удаление фото", "set_online": "В сети",
                            "check_restriction": "Проверка ограничений"}[op]
        return None, "Неизвестная операция"

    async def accounts_mass(request: web.Request) -> web.Response:
        """Массовое действие над выбранными аккаунтами.
        body: {op: check|scan|leave_all|name|avatar|2fa, account_ids: [..], ...}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        op = body.get("op")
        # По умолчанию масс-операция идёт по СВОИМ аккаунтам (даже у админа) —
        # чтобы админ случайно не оперировал над чужими. Платформенный срез —
        # только явным {"scope":"platform"} в теле.
        admin = _body_wants_platform_scope(uid, body)
        # Режим «применить ко всему срезу»: id берём не из загруженной страницы,
        # а резолвим весь набор под текущим фильтром на сервере (тот же WHERE, что
        # и список) со скоупом owner/admin. Снимает 100-лимит для масс-операций.
        if body.get("select_all_filtered"):
            flt = body.get("filter", "all")
            if flt not in ACCOUNT_FILTERS:
                flt = "all"
            stage = (body.get("stage") or "").strip().lower()
            if stage and stage not in ACCOUNT_STAGES:
                stage = ""
            qterm = (body.get("q") or "").strip()[:64]
            # Срез по пулу обязан доехать и сюда: иначе «применить ко всему
            # срезу» взяло бы аккаунты вне показанного пула — операция ушла бы
            # шире, чем видел владелец.
            bpool = (body.get("pool") or "").strip()[:64]
            # То же и про срез по стране: иначе «применить ко всему срезу»
            # ушло бы по всему флоту, а владелец видел одну страну.
            bgeo = _geo_code(body.get("geo"))
            where, wargs = _accounts_where(uid, flt, stage, qterm, admin=admin,
                                           acc_pool=bpool, geo=bgeo)
            owned = await _safe_fetch(pool,
                f"SELECT id FROM tg_accounts WHERE {where} ORDER BY id LIMIT 5000", *wargs)
            ids = [int(r["id"]) for r in (owned or [])]
            if not ids:
                return _err("Под фильтром нет аккаунтов", 404)
        else:
            ids_in = [int(x) for x in (body.get("account_ids") or []) if str(x).lstrip("-").isdigit()]
            if not ids_in:
                return _err("Выберите аккаунты", 400)
            if admin:
                owned = await _safe_fetch(pool,
                    "SELECT id FROM tg_accounts WHERE id = ANY($1::bigint[])",
                    ids_in)
            else:
                owned = await _safe_fetch(pool,
                    "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
                    uid, ids_in)
            ids = [int(r["id"]) for r in (owned or [])]
            if not ids:
                return _err("Аккаунты не найдены", 404)
        n = len(ids)
        try:
            if op == "check":
                _params = {"account_ids": ids, "check_spambot": True}
                if admin:
                    # См. accounts_check: признак ставится только после проверки
                    # прав на платформенный срез.
                    _params["cross_owner"] = True
                op_id = await _obus.submit(
                    pool, uid, "check_accounts_health", _params,
                    total_items=n, label=f"Проверка · аккаунтов: {n}")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            if op == "scan":
                op_id = await _obus.submit(
                    pool, uid, "scan_owned_resources", {"account_ids": ids},
                    total_items=n, label=f"Скан ресурсов: {n} акк.")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            if op == "set_stage":
                # Пере­мещение в CRM-статус — чистое DB-действие, без очереди/Telethon.
                raw = body.get("stage")
                stage = (str(raw).strip().lower() or None) if raw is not None else None
                if stage is not None and stage not in ACCOUNT_STAGES:
                    return _err("Неизвестный статус", 400)
                await pool.execute(
                    "UPDATE tg_accounts SET stage=$1 WHERE owner_id=$2 AND id=ANY($3::bigint[])",
                    stage, uid, ids)
                return _json_resp({"ok": True, "count": n, "stage": stage})
            if op == "detach_proxy":
                # Снять прокси у выбранных аккаунтов — чтобы работать БЕЗ прокси
                # (прямое соединение). Чистое DB-действие. После этого аккаунт
                # не привязан к IP прокси и подключается напрямую.
                await pool.execute(
                    "UPDATE tg_accounts SET proxy_id=NULL WHERE owner_id=$1 AND id=ANY($2::bigint[])",
                    uid, ids)
                return _json_resp({"ok": True, "count": n})
            if op == "delete":
                # Жёсткое удаление аккаунтов (напр. массово — невоскрешаемые:
                # фильтр 'dead'). Необратимо. Связанные строки: часть таблиц имеет
                # FK ON DELETE CASCADE/SET NULL (trust/health history, flood_log,
                # gifts и т.п.) и чистится сразу; таблицы БЕЗ FK
                # (account_status_events/daily_stats/rehab_state) остаются висячими,
                # но не обрабатываются (consumers join'ят tg_accounts) и подчищаются
                # фоново в db_maintenance. Скоуп уже применён при резолве ids
                # (owner/admin), поэтому чужие не заденем.
                res = await pool.execute(
                    "DELETE FROM tg_accounts WHERE id=ANY($1::bigint[])"
                    + ("" if admin else " AND owner_id=$2"),
                    ids, *([] if admin else [uid]))
                deleted = int(str(res).rsplit(" ", 1)[-1]) if str(res).startswith("DELETE") else n
                # Массовое удаление идёт сырым запросом мимо db.remove_tg_account,
                # поэтому журнал пишем здесь: иначе на вопрос «куда делись сорок
                # аккаунтов» ответа в системе нет, а удалить их мог и напарник по
                # workspace.
                if deleted:
                    from database.db import record_manual_action

                    await record_manual_action(
                        pool, uid, "accounts_bulk_delete",
                        target=",".join(str(i) for i in ids[:50]))
                return _json_resp({"ok": True, "count": deleted})
            if op == "leave_all":
                # leave_all_chats — по одному аккаунту, ставим N операций
                op_ids = []
                for aid in ids:
                    oid = await _obus.submit(
                        pool, uid, "leave_all_chats", {"account_id": aid},
                        total_items=1, label=f"Выход из всех чатов (акк. {aid})")
                    op_ids.append(int(oid))
                return _json_resp({"ok": True, "op_ids": op_ids, "count": n})
            if op in ("name", "avatar", "2fa", "username", "bio", "close_sessions", "privacy",
                      "clear_bio", "remove_username", "remove_avatar", "reset_2fa",
                      "set_online", "check_restriction"):
                params, label = _build_profile_params(op, body, ids)
                if params is None:
                    return _err(label, 400)
                op_id = await _obus.submit(
                    pool, uid, "profile_setter", params,
                    total_items=n, label=f"{label}: {n} акк.")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            return _err("Неизвестная операция", 400)
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("accounts_mass uid=%d op=%s", uid, op)
            return _err(_INTERNAL_ERROR, 500)

    async def account_profile(request: web.Request) -> web.Response:
        """Сменить профиль аккаунта: имя/bio | аватар | 2FA (op).
        Маппится на op_type=profile_setter (контракт _exec_bulk_set_profile)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        op = body.get("op")
        params: dict = {"op": op, "account_ids": [acc_id]}
        if op == "name":
            fn = (body.get("first_name") or "").strip()
            if not fn:
                return _err("Укажите имя", 400)
            params["name_data"] = {
                "first_name": fn,
                "last_name": (body.get("last_name") or "").strip(),
                "about": (body.get("about") or "").strip(),
            }
            label = "Смена имени/bio"
        elif op == "avatar":
            url = (body.get("avatar_url") or "").strip()
            if not url:
                return _err("Укажите ссылку на аватар", 400)
            params["avatar_url"] = url
            label = "Смена аватара"
        elif op == "2fa":
            np = (body.get("new_password") or "").strip()
            if not np:
                return _err("Укажите новый пароль", 400)
            params["new_password"] = np
            params["current_password"] = (body.get("current_password") or "").strip()
            params["hint"] = (body.get("hint") or "").strip()
            label = "Смена 2FA"
        elif op == "username":
            un = (body.get("username") or "").strip().lstrip("@")
            if not un:
                return _err("Укажите username", 400)
            params["username"] = un
            label = "Смена username"
        elif op == "close_sessions":
            label = "Закрыть сторонние сессии"
        elif op == "privacy":
            pk = (body.get("privacy_key") or "phone").strip()
            if pk not in ("phone", "invite", "lastseen"):
                return _err("Настройка приватности: phone, invite или lastseen", 400)
            params["privacy_key"] = pk
            params["privacy_allow"] = bool(body.get("privacy_allow", False))
            label = f"Приватность: {pk}"
        elif op == "reset_2fa":
            cp = (body.get("current_password") or "").strip()
            if not cp:
                return _err("Укажите текущий пароль 2FA для снятия", 400)
            params["current_password"] = cp
            label = "Снятие 2FA"
        elif op in ("clear_bio", "remove_username", "remove_avatar", "set_online", "check_restriction"):
            label = {"clear_bio": "Очистка bio", "remove_username": "Снятие username",
                     "remove_avatar": "Удаление фото", "set_online": "В сети",
                     "check_restriction": "Проверка ограничений"}[op]
        else:
            return _err("Неизвестная операция", 400)
        # Единичный аккаунт — исполняем ИНЛАЙН с немедленным результатом (как
        # check_restriction). Раньше op просто ставился в очередь и молча падал в
        # фоне при мёртвой сессии/прокси → пользователь видел «ничего не происходит».
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import profile_setter_engine as pse
            res = await asyncio.wait_for(
                pse.apply_op(acc["session_str"], dict(acc), op, params), timeout=45)
            if res.get("ok"):
                return _json_resp({"ok": True, "result": res, "label": label})
            return _err(res.get("error") or "Операция не выполнена", 400)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 45с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_profile inline uid=%d acc=%d op=%s", uid, acc_id, op)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)

    async def account_login_code(request: web.Request) -> web.Response:
        """Получить последний код входа Telegram для аккаунта (инлайн, из чата 777000).
        Аналог «Получить код авторизации» — раньше в mini-app недоступно."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import profile_setter_engine as pse
            # Таймаут на инлайн-коннект — иначе зависание → edge 502/520.
            res = await asyncio.wait_for(
                pse.get_login_code(acc["session_str"], {**dict(acc), "_low_risk": True}), timeout=30
            )
            if res.get("ok"):
                return _json_resp({"ok": True, "code": res["code"]})
            return _err(res.get("error") or "Код не найден", 404)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 30с — проверьте прокси/сессию", 400)
        except Exception:
            log.exception("account_login_code uid=%d acc=%d", uid, acc_id)
            return _err("Ошибка получения кода", 400)

    async def account_check_restriction(request: web.Request) -> web.Response:
        """Инлайн-проверка ограничений одного аккаунта (жив/ограничен/удалён).
        Аналог раздела «ПРОВЕРКА» Telegram Expert — результат сразу, без очереди."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import profile_setter_engine as pse
            # Инлайн-коннект к Telegram с ЖЁСТКИМ таймаутом: без него зависший
            # коннект (мёртвая сессия/плохой прокси) висит до edge-таймаута и
            # отдаёт 502/520 → фронт показывает «Сервис временно недоступен».
            res = await asyncio.wait_for(
                pse.check_restriction(acc["session_str"], {**dict(acc), "_low_risk": True}), timeout=30
            )
            if not res.get("ok"):
                # Бизнес-ошибка (не смогли проверить), НЕ 502 — иначе фронт покажет
                # «сервис недоступен» вместо реальной причины.
                return _err(res.get("error") or "Не удалось проверить аккаунт", 400)
            return _json_resp({
                "ok": True,
                "alive": res.get("alive"),
                "restricted": res.get("restricted"),
                "deleted": res.get("deleted"),
                "reason": res.get("reason"),
                "username": res.get("username"),
                "user_id": res.get("user_id"),
            })
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 30с — проверьте прокси/сессию", 400)
        except Exception:
            log.exception("account_check_restriction uid=%d acc=%d", uid, acc_id)
            return _err("Ошибка проверки аккаунта", 400)

    async def accounts_export_json(request: web.Request) -> web.Response:
        """Экспорт МЕТАДАННЫХ аккаунтов пользователя в JSON (аналог «РАБОТА С JSON»).
        Секреты (session_str, proxy-креды) НАМЕРЕННО не выгружаются — иначе это
        обнулило бы шифрование at-rest (см. AUDIT_LEDGER, разрыв №1). Экспортируем
        то, что реально нужно для учёта: id, телефон, имя, username, кластер, статус,
        есть ли прокси. Импорт tdata/session — отдельный защищённый путь."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        base = ("SELECT id, phone, first_name, username, cluster, acc_status, "
                "real_check_status, warmup_level, is_active, "
                "(proxy_id IS NOT NULL) AS has_proxy, added_at "
                "FROM tg_accounts WHERE owner_id=$1 ORDER BY id")
        try:
            rows = await pool.fetch(base, uid)
        except Exception:
            # оборонительный фолбэк на гарантированно существующие колонки
            rows = await _safe_fetch(pool,
                "SELECT id, phone, first_name, username, is_active, "
                "(proxy_id IS NOT NULL) AS has_proxy FROM tg_accounts "
                "WHERE owner_id=$1 ORDER BY id", uid)
        out = []
        for r in (rows or []):
            d = dict(r)
            ca = d.get("added_at")
            if hasattr(ca, "isoformat"):
                d["added_at"] = ca.isoformat()
            out.append(d)
        return _json_resp({"ok": True, "count": len(out), "accounts": out})

    async def account_toggle(request: web.Request) -> web.Response:
        """Вкл/выкл аккаунта (is_active)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        try:
            row = await pool.fetchrow(
                "SELECT is_active FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
            if not row:
                return _err("Аккаунт не найден", 404)
            new_state = not bool(row["is_active"])
            await pool.execute(
                "UPDATE tg_accounts SET is_active=$1 WHERE id=$2 AND owner_id=$3",
                new_state, acc_id, uid)
            return _json_resp({"ok": True, "is_active": new_state})
        except Exception:
            log.exception("account_toggle uid=%d acc=%d", uid, acc_id)
            return _err(_INTERNAL_ERROR, 500)

    async def account_delete(request: web.Request) -> web.Response:
        """Удалить аккаунт."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        try:
            res = await pool.execute(
                "DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
            if str(res).endswith(" 0"):
                return _err("Аккаунт не найден", 404)
            from database.db import record_manual_action

            await record_manual_action(
                pool, uid, "account_delete", target=str(acc_id))
            return _json_resp({"ok": True})
        except Exception:
            log.exception("account_delete uid=%d acc=%d", uid, acc_id)
            return _err(_INTERNAL_ERROR, 500)

    async def account_action(request: web.Request) -> web.Response:
        """Операция от имени одного аккаунта: scan | leave_all.
        Маппится на существующие op_type (scan_owned_resources / leave_all_chats)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            act = request.match_info["act"]
        except (KeyError, ValueError):
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        if act == "scan":
            op_type, params, label = "scan_owned_resources", {"account_ids": [acc_id]}, "Скан ресурсов аккаунта"
        elif act == "leave_all":
            op_type, params, label = "leave_all_chats", {"account_id": acc_id}, "Выход из всех чатов"
        elif act == "read_all":
            op_type, params, label = "read_all_dialogs", {"account_id": acc_id}, "Прочитать все диалоги"
        elif act == "delete_pm":
            op_type, params, label = "delete_private_dialogs", {"account_id": acc_id}, "Удаление личных диалогов"
        elif act == "reset_cooldown":
            # СИНХРОННО (раньше был фантомный op reset_cooldown без исполнителя →
            # кнопка ничего не делала). Чистим durable-кулдаун И снимаем карантин
            # риск-пульса: раньше сбрасывался только cooldown_until, а карантин по
            # restriction_events держал аккаунт вне операций — владелец видел
            # «кулдаун не сбрасывается». risk_cleared_at=NOW() говорит
            # is_account_quarantined считать только НОВЫЕ ограничения (история
            # событий цела; свежее ограничение снова уведёт в карантин).
            #
            # Полный сброс — пять шагов (кулдаун в БД и в памяти flood_engine,
            # отметка риска, транзиентный acc_status, запрет suitability), и он
            # ОДИН на все кнопки: services/account_reset.py. Раньше эта кнопка,
            # кнопка бота и массовый сброс были написаны порознь и пропускали
            # разные шаги — результат зависел от того, откуда владелец нажал.
            try:
                from services.account_reset import reset_account
                await reset_account(pool, acc_id, uid)
                return _json_resp({"ok": True, "message": "⚡ Кулдаун и риск-карантин сброшены"})
            except Exception:
                log.exception("reset_cooldown uid=%d acc=%d", uid, acc_id)
                return _err(_INTERNAL_ERROR, 500)
        elif act == "export_session":
            # СИНХРОННО (раньше фантомный op → r.session никогда не приходил).
            # Владелец экспортирует СВОЮ сессию (owner-scoped), расшифровываем.
            row = await _safe_fetchrow(pool,
                "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
            if not row or not row["session_str"]:
                return _err("Сессия недоступна", 404)
            try:
                from services.token_vault import decrypt_token
                sess = decrypt_token(row["session_str"])
            except Exception:
                sess = row["session_str"]
            return _json_resp({"ok": True, "session": sess})
        elif act == "reauth":
            # Переавторизация требует ОДНОРАЗОВЫЙ КОД из Telegram (вводится вручную) —
            # фоновой операцией невозможно. Раньше ставился фантомный op reauth_account
            # (нет исполнителя) → «запущена», но ничего не происходило. Честно ведём в
            # бота, где flow релога реально работает (шлёт код → приём кода → вход).
            return _json_resp({"ok": True, "message": (
                "🔑 Переавторизация требует код из Telegram (вводится вручную), поэтому "
                "выполняется в боте: Аккаунты → 🔄 Переавторизатор → выберите этот аккаунт "
                "→ Релог. После входа аккаунт снова станет активным.")})
        else:
            return _err("Неизвестное действие", 400)
        try:
            op_id = await _obus.submit(
                pool, uid, op_type, params, total_items=1, label=label)
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("account_action uid=%d acc=%d act=%s", uid, acc_id, act)
            return _err(_INTERNAL_ERROR, 500)

    async def account_post_story(request: web.Request) -> web.Response:
        """Story Manager: опубликовать историю на СВОЙ аккаунт из media_url.
        body: {media_url, caption?, period_hours?}. Инлайн, немедленный итог."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        media_url = (body.get("media_url") or "").strip()
        if not media_url:
            return _err("Укажите ссылку на медиа (фото/видео)", 400)
        caption = (body.get("caption") or "").strip()
        period_hours = body.get("period_hours", 24)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import story_manager
            res = await asyncio.wait_for(
                story_manager.post_story(acc["session_str"], media_url, caption,
                                         period_hours, dict(acc), pool=pool),
                timeout=180)
        except asyncio.TimeoutError:
            return _err("Публикация не завершилась за 180с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_post_story uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if res.get("ok"):
            return _json_resp({"ok": True, "status": res.get("status"),
                               "label": f"✅ История опубликована ({res.get('period_hours', 24)}ч)"})
        return _err(res.get("error") or "Не удалось опубликовать историю", 400)

    def _parse_access_hash(raw) -> int | None:
        """access_hash из query/body/form → int|None. Позволяет резолвить
        собеседника (контакт) без общего чата; пустое/мусор → None (безопасно)."""
        if raw is None:
            return None
        s = str(raw).strip()
        if s and s.lstrip("-").isdigit():
            return int(s)
        return None

    async def _console_account(uid: int, acc_id: int):
        """Загрузить сессию+транспорт аккаунта для консоли, скоуп по владельцу.

        Транспорт берём КАНОНИЧЕСКИМ загрузчиком `db.get_account_for_telethon`.
        Свой SELECT здесь брал только session_str, отпечаток устройства и
        proxy_url, а `_make_client` выбирает выход по четырём полям: назначенный
        прокси, `cf_relay_url`, политика прокси владельца (`owner_id` →
        `proxy_policy`) и его IPv6-подсеть. Трёх последних в словаре не было,
        поэтому консоль уводила аккаунт ДРУГИМ выходом, чем массовые операции:
        владелец с политикой strict или со своей IPv6-подсетью получал одну
        сессию с двух адресов. Это AUTH_KEY_DUPLICATED — Telegram отзывает ключ
        навсегда, а снаружи это выглядит как «аккаунты сами отваливаются».

        Внешние данные (тексты, имена) — это ДАННЫЕ: экранирует их фронт, бэкенд
        отдаёт сырьё. session_str наружу не отдаём и не логируем.
        """
        # Отдельной строкой, потому что канонический загрузчик не фильтрует
        # is_active: выключенный аккаунт консоли не отдаём.
        alive = await _safe_fetchval(
            pool,
            "SELECT 1 FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            acc_id, uid)
        if not alive:
            return None
        from database import db as _db
        return await _db.get_account_for_telethon(pool, acc_id, uid)

    async def account_dialogs(request: web.Request) -> web.Response:
        """Список диалогов аккаунта (только чтение)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        acc = await _console_account(uid, acc_id)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import account_console
            res = await asyncio.wait_for(
                account_console.list_dialogs(acc["session_str"], dict(acc), limit=50,
                                             pool=pool),
                timeout=60)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 60с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_dialogs uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось получить диалоги", 400)
        return _json_resp({"ok": True, "dialogs": res.get("dialogs", [])})

    async def account_dialog_history(request: web.Request) -> web.Response:
        """История одного диалога аккаунта (только чтение)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        from services import account_console
        peer = account_console.parse_peer(request.match_info.get("peer", ""))
        if peer == "" or peer is None:
            return _err("Не указан собеседник", 400)
        access_hash = _parse_access_hash(request.query.get("access_hash"))
        acc = await _console_account(uid, acc_id)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            res = await asyncio.wait_for(
                account_console.get_history(acc["session_str"], dict(acc), peer,
                                            limit=50, access_hash=access_hash,
                                            pool=pool),
                timeout=60)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 60с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_dialog_history uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось получить историю", 400)
        return _json_resp({"ok": True, "peer_name": res.get("peer_name"),
                           "messages": res.get("messages", [])})

    async def account_dialog_send(request: web.Request) -> web.Response:
        """Отправить текстовое сообщение в диалог (ручное действие 1:1)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        from services import account_console
        peer = account_console.parse_peer(request.match_info.get("peer", ""))
        if peer == "" or peer is None:
            return _err("Не указан собеседник", 400)
        text = _text(body.get("text"))
        if not text:
            return _err("Введите текст сообщения", 400)
        access_hash = _parse_access_hash(body.get("access_hash"))
        acc = await _console_account(uid, acc_id)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            res = await asyncio.wait_for(
                account_console.send_text(acc["session_str"], dict(acc), peer, text,
                                          access_hash=access_hash, pool=pool),
                timeout=60)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 60с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_dialog_send uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось отправить", 400)
        return _json_resp({"ok": True, "message_id": res.get("message_id")})

    async def account_dialog_send_file(request: web.Request) -> web.Response:
        """Отправить файл (фото/документ) в диалог (ручное действие 1:1).

        multipart: file (обязательно), caption?, access_hash?. Лимит 20 МБ —
        защита от OOM; читаем чанками.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        from services import account_console
        peer = account_console.parse_peer(request.match_info.get("peer", ""))
        if peer == "" or peer is None:
            return _err("Не указан собеседник", 400)
        try:
            reader = await request.multipart()
        except Exception:
            return _err("Неверный запрос", 400)
        file_bytes = b""
        filename = "file"
        caption = ""
        access_hash = None
        _MAX = 20 * 1024 * 1024
        async for part in reader:
            if part.name == "file":
                filename = part.filename or "file"
                buf = bytearray()
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > _MAX:
                        return _err("Файл слишком большой (макс 20 МБ)", 400)
                file_bytes = bytes(buf)
            elif part.name == "caption":
                caption = (await read_upload_text(part))[:1024]
            elif part.name == "access_hash":
                access_hash = _parse_access_hash((await read_upload_text(part)).strip())
        if not file_bytes:
            return _err("Файл не получен", 400)
        acc = await _console_account(uid, acc_id)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            res = await asyncio.wait_for(
                account_console.send_file(acc["session_str"], dict(acc), peer,
                                          file_bytes, filename, caption, access_hash,
                                          pool=pool),
                timeout=180)
        except asyncio.TimeoutError:
            return _err("Отправка файла не завершилась за 180с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_dialog_send_file uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось отправить файл", 400)
        return _json_resp({"ok": True, "message_id": res.get("message_id")})

    async def account_contacts(request: web.Request) -> web.Response:
        """Контакты аккаунта — адресная книга Telegram (только чтение)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        acc = await _console_account(uid, acc_id)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import account_console
            res = await asyncio.wait_for(
                account_console.list_contacts(acc["session_str"], dict(acc), pool=pool),
                timeout=60)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 60с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_contacts uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось получить контакты", 400)
        return _json_resp({"ok": True, "contacts": res.get("contacts", [])})

    async def account_channels(request: web.Request) -> web.Response:
        """Каналы/чаты, ПРИВЯЗАННЫЕ К ЭТОМУ аккаунту (managed_channels.acc_id).

        Именно это «ресурсы аккаунта»: только каналы, где acc_id = выбранный
        аккаунт, а не весь портфель владельца. Скоуп по owner_id И acc_id.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        rows = await _safe_fetch(pool,
            "SELECT channel_id AS id, channel_id, username, title, type, "
            "COALESCE(members_count, 0) AS member_count "
            "FROM managed_channels WHERE owner_id=$1 AND acc_id=$2 "
            "ORDER BY member_count DESC NULLS LAST LIMIT 500", uid, acc_id)
        return _json_resp({"channels": rows, "total": len(rows or [])})

    async def account_bots(request: web.Request) -> web.Response:
        """Боты, СОЗДАННЫЕ через этот аккаунт (managed_bots.acc_id).

        Боты не привязаны к аккаунту в общем случае (добавляются по токену
        владельцем), но созданные в Bot Factory через конкретный аккаунт несут
        acc_id — их и показываем. Скоуп по added_by (владелец) И acc_id.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        rows = await _safe_fetch(pool,
            "SELECT bot_id, username, first_name, is_active FROM managed_bots "
            "WHERE added_by=$1 AND acc_id=$2 ORDER BY added_at DESC LIMIT 500", uid, acc_id)
        return _json_resp({"bots": rows, "total": len(rows or [])})

    async def account_spamblock_appeal(request: web.Request) -> web.Response:
        """Снятие спамблока: запрос в @SpamBot с проходом по кнопкам аппеляции.
        Инлайн, немедленный результат (реабилитация своего аккаунта)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import account_manager
            res = await asyncio.wait_for(
                account_manager.appeal_spamblock(acc["session_str"], dict(acc)), timeout=60)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 60с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_spamblock_appeal uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        # Синхронизируем acc_status в БД, если бот подтвердил «free».
        if res.get("status") == "free":
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET acc_status='ok' WHERE id=$1 AND owner_id=$2", acc_id, uid)
            except Exception:
                pass
        labels = {"free": "✅ Спамблок снят — аккаунт активен",
                  "appeal_sent": "📨 Аппеляция отправлена — ждите решения",
                  "still_blocked": "🔴 Спамблок активен, кнопки аппеляции нет",
                  "error": "⚠️ Ошибка обращения к @SpamBot",
                  "no_session": "⚠️ Сессия недоступна"}
        if not res.get("ok") and res.get("status") not in ("still_blocked", "appeal_sent", "free"):
            return _err(labels.get(res.get("status"), "Не выполнено") + (f": {res.get('reply')}" if res.get("reply") else ""), 400)
        return _json_resp({"ok": True, "status": res.get("status"),
                           "label": labels.get(res.get("status"), res.get("status")),
                           "reply": res.get("reply"), "steps": res.get("steps", 0)})

    async def account_set_proxy(request: web.Request) -> web.Response:
        """Назначить/снять прокси у аккаунта. body: {proxy_id: int|null}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        raw = body.get("proxy_id")
        proxy_id = None
        if raw not in (None, "", 0, "0"):
            try:
                proxy_id = int(raw)
            except (TypeError, ValueError):
                return _err("Неверный идентификатор прокси", 400)
            owns_proxy = await _safe_count(pool,
                "SELECT COUNT(*) FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, uid)
            if not owns_proxy:
                return _err("Прокси не найден", 404)
        try:
            await pool.execute(
                "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3",
                proxy_id, acc_id, uid)
            return _json_resp({"ok": True, "proxy_id": proxy_id})
        except Exception:
            log.exception("account_set_proxy uid=%d acc=%d", uid, acc_id)
            return _err(_INTERNAL_ERROR, 500)

    async def account_set_note(request: web.Request) -> web.Response:
        """Заметка к аккаунту (account_notes)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        note = validate_string(body.get("note"), max_len=500, required=False)
        try:
            res = await pool.execute(
                "UPDATE tg_accounts SET account_notes=$1 WHERE id=$2 AND owner_id=$3",
                note or None, acc_id, uid)
            if str(res).endswith(" 0"):
                return _err("Аккаунт не найден", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("account_set_note uid=%d acc=%d", uid, acc_id)
            return _err(_INTERNAL_ERROR, 500)

    async def account_set_meta(request: web.Request) -> web.Response:
        """Привязка аккаунта к кластеру и/или переименование метки (first_name)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        sets: list = []
        args: list = []
        idx = 1
        if "cluster" in body:
            raw = body.get("cluster")
            cluster = validate_string(raw, max_len=64, required=False)
            sets.append(f"cluster=${idx}"); args.append(cluster); idx += 1
        if "label" in body:
            label = validate_string(body.get("label"), max_len=128)
            if not label:
                return _err("Метка не может быть пустой", 400)
            sets.append(f"first_name=${idx}"); args.append(label); idx += 1
        if "stage" in body:
            raw = body.get("stage")
            stage = validate_string(raw, max_len=20, required=False)
            if stage:
                stage = stage.lower()
            if stage is not None and stage not in ACCOUNT_STAGES:
                return _err("Неизвестный статус", 400)
            sets.append(f"stage=${idx}"); args.append(stage); idx += 1
        if not sets:
            return _err("Нечего обновлять", 400)
        args.extend([acc_id, uid])
        sql = (f"UPDATE tg_accounts SET {', '.join(sets)} "
               f"WHERE id=${idx} AND owner_id=${idx+1}")
        try:
            res = await pool.execute(sql, *args)
            if str(res).endswith(" 0"):
                return _err("Аккаунт не найден", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("account_set_meta uid=%d acc=%d", uid, acc_id)
            return _err(_INTERNAL_ERROR, 500)

    async def account_check_one(request: web.Request) -> web.Response:
        """Проверить один аккаунт (с реактивацией если рабочий)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        try:
            op_id = await _obus.submit(
                pool, uid, "check_accounts_health",
                {"account_ids": [acc_id], "check_spambot": True},
                total_items=1, label="Проверка аккаунта")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("account_check_one uid=%d acc=%d", uid, acc_id)
            return _err(_INTERNAL_ERROR, 500)

    async def accounts_revive(request: web.Request) -> web.Response:
        """«Поднять флот»: снять операционные блокировки со ВСЕХ аккаунтов
        владельца, у которых есть сессия, чтобы недавно переподключённый флот
        сразу заработал.

        Переимпорт чинит это на будущее (add_tg_account), но уже переподключённые
        аккаунты сохранили залипшие блокировки от прошлой (сожжённой) сессии:
        in_operation (исключал из выборки/прогрева), cooldown_until (старый
        кулдаун), trust_score=0 (крит-обнуление не проходило min_trust-гейт),
        is_active=FALSE / acc_status. Чистим их одним апдейтом. Оптимистично
        активируем — если сессия физически мертва, монитор пере-пометит её
        session_expired на следующем цикле (безопасно, как и при импорте).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)

        # Аккаунты, которыми ПРЯМО СЕЙЧАС работает операция, не трогаем.
        # Снять с такого блокировку значит отдать живую сессию второму
        # исполнителю: `_db_claim` пускает по аренде, аренда снята — аккаунт
        # свободен, и две операции коннектятся одним auth-key
        # (AUTH_KEY_DUPLICATED, сессия сгорает). Спрашиваем оба источника
        # правды: аренду в БД (её держит любой процесс) и память этого
        # процесса (там же прогрев, призрак и пре-флайт).
        #
        # FAIL-CLOSED: не смогли узнать, кто занят — не поднимаем ничего.
        # Цена отказа — владелец нажмёт кнопку снова; цена ошибки —
        # сожжённая сессия.
        _own_ids: list[int] = []
        busy: set[int] = set()
        try:
            _own = await pool.fetch(
                "SELECT id, (op_lease_until IS NOT NULL "
                "            AND op_lease_until > now()) AS leased "
                "  FROM tg_accounts "
                " WHERE owner_id=$1 AND session_str IS NOT NULL "
                "   AND session_str != ''",
                uid,
            )
            _own_ids = [int(r["id"]) for r in (_own or [])]
            busy = {int(r["id"]) for r in (_own or []) if r["leased"]}
            from services import op_worker as _opw
            busy |= await _opw.accounts_busy_now(_own_ids)
        except Exception:
            log.exception("accounts_revive uid=%s: проверка занятости", uid)
            return _err(
                "Не удалось проверить, какие аккаунты заняты операцией — "
                "флот не тронут, попробуйте ещё раз", 500)

        try:
            res = await pool.execute(
                # op_lease_until/op_lease_owner гасим вместе с флагом: снятый
                # флаг при живой аренде — это и есть то состояние, в котором
                # аккаунт нельзя ни взять, ни продлить (см. _db_claim).
                # Условие по аренде дублирует список busy намеренно: между
                # SELECT выше и этим UPDATE операция может успеть взять
                # аккаунт, и защищает здесь именно SQL, а не снимок.
                """UPDATE tg_accounts
                   SET is_active=TRUE,
                       acc_status='active',
                       status_reason=NULL,
                       in_operation=FALSE,
                       op_lease_until=NULL,
                       op_lease_owner=NULL,
                       cooldown_until=NULL,
                       trust_score=GREATEST(COALESCE(trust_score, 1.0), 1.0),
                       flood_count_7d=0
                   WHERE owner_id=$1
                     AND session_str IS NOT NULL AND session_str != ''
                     AND NOT (id = ANY($2::int[]))
                     AND (op_lease_until IS NULL OR op_lease_until < now())
                     AND (
                           COALESCE(is_active, FALSE) = FALSE
                        OR COALESCE(in_operation, FALSE) = TRUE
                        OR cooldown_until IS NOT NULL
                        OR COALESCE(acc_status, 'active') <> 'active'
                        OR COALESCE(trust_score, 1.0) < 1.0
                     )""",
                uid, sorted(busy),
            )
            try:
                revived = int(str(res).split()[-1])
            except (ValueError, IndexError):
                revived = 0
            # Снимаем cf_relay_url с аккаунтов, чей CF-воркер лёг (нет в живом пуле):
            # иначе безпроксёвый аккаунт при каждом коннекте тратит таймаут на
            # мёртвый релей, прежде чем откатиться на прямой host-IP. Best-effort
            # (таблица cf_worker_pool может отсутствовать на старой схеме).
            try:
                cleared = await pool.execute(
                    "UPDATE tg_accounts SET cf_relay_url=NULL "
                    "WHERE owner_id=$1 AND proxy_id IS NULL AND cf_relay_url IS NOT NULL "
                    "AND cf_relay_url NOT IN (SELECT worker_url FROM cf_worker_pool "
                    "  WHERE owner_id=$1 AND status <> 'down')",
                    uid)
                log.info("accounts_revive uid=%s: cleared dead relays %s", uid, cleared)
            except Exception:
                log.debug("accounts_revive: dead-relay clear skipped", exc_info=True)
            # Снимаем и in-memory lock op_worker'а (иначе арбитр держал бы «занят»
            # до рестарта, и revive из БД не помог бы операции их захватить).
            try:
                from services import op_worker as _opw
                # Занятые — мимо: release_accounts снимает и аренду в БД, то
                # есть вернул бы ровно то состояние, от которого защищает
                # UPDATE выше.
                await _opw.release_accounts(
                    [i for i in _own_ids if i not in busy])
            except Exception:
                log.debug("accounts_revive: op_worker release skipped", exc_info=True)
            return _json_resp(
                {"ok": True, "revived": revived, "busy_skipped": len(busy)})
        except Exception:
            log.exception("accounts_revive uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def start_warmup(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        plan_type = body.get("plan_type", "standard")
        if plan_type not in ("standard", "gentle", "aggressive"):
            plan_type = "standard"
        # Values must match account_warmer.create_warmup_plan() — aggressive capped at 12/day
        target_days    = {"gentle": 21, "standard": 14, "aggressive": 10}[plan_type]
        daily_actions  = {"gentle":  5, "standard": 10, "aggressive": 12}[plan_type]
        # Require session_str so the worker doesn't fail immediately
        has_session = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND session_str IS NOT NULL",
            acc_id, uid)
        if not has_session:
            return _err("Аккаунт не имеет активной сессии — сначала добавьте .session файл", 400)
        # Cancel any active warmup first
        try:
            # Гейт тарифа ДО записи плана: раньше план ставился первым, а отказ
            # submit() по тарифу оставлял «активный» план без операции, который
            # фоновый цикл грел без подписки.
            await _obus.enforce_gates(pool, uid, "account_warmup")
            await pool.execute(
                "UPDATE account_warmup_plans SET status='paused' WHERE account_id=$1 AND owner_id=$2 AND status='active'",
                acc_id, uid)
            row = await pool.fetchrow(
                """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (account_id) DO UPDATE
                   SET plan_type=$3, target_days=$4, daily_actions=$5, status='active', started_at=NOW()
                   RETURNING id""",
                uid, acc_id, plan_type, target_days, daily_actions)
            # Also create op_queue entry so op_worker executes warmup logic
            label = f"Прогрев аккаунта #{acc_id} ({plan_type})"
            op_id = await _obus.submit(
                pool, uid, "account_warmup",
                {"account_id": acc_id, "plan_type": plan_type},
                total_items=1, label=label)
            return _json_resp({"ok": True, "id": row["id"], "op_id": op_id, "target_days": target_days})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("start_warmup acc=%d uid=%d", acc_id, uid)
            log.exception("warmup_start")
            return _err("Не удалось запустить прогрев", 500)

    async def pause_warmup(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans SET status='paused'
                   WHERE account_id=$1 AND owner_id=$2 AND status='active'
                   RETURNING id""", acc_id, uid)
            if not row:
                return _err("Активного прогрева нет", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("pause_warmup")
            return _err("Не удалось приостановить", 500)

    async def accounts_health(request: web.Request) -> web.Response:
        """Единый риск-пульс аккаунтов владельца (иммунные сигналы → один сигнал)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.infra_memory import get_account_health
            return _json_resp(await get_account_health(pool, uid))
        except Exception:
            log.exception("accounts_health uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _resolve_proxy_url(_uid: int, proxy_id) -> tuple[int | None, str | None]:
        """proxy_id → (validated_proxy_id, proxy_url) со скоупом по владельцу."""
        try:
            pid = int(proxy_id) if proxy_id else None
        except (TypeError, ValueError):
            return None, None
        if not pid:
            return None, None
        row = await _safe_fetchrow(pool,
            "SELECT proxy_url FROM user_proxies WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            pid, _uid)
        return (pid, row["proxy_url"]) if row else (None, None)

    async def _persist_login(_uid: int, session_str: str, info: dict, pid: int | None) -> int:
        """Сохранить залогиненный аккаунт (шифрование+дедуп) + закрепить прокси."""
        from database import db as _db
        acc_id = await _db.add_tg_account(
            pool, owner_id=_uid,
            phone=info.get("phone") or "",
            session_str=session_str,
            tg_user_id=info.get("tg_user_id"),
            first_name=info.get("first_name", ""),
            username=info.get("username", ""),
            device_model=info.get("device_model"),
            system_version=info.get("system_version"),
            app_version=info.get("app_version"),
            lang_code=info.get("lang_code"),
            system_lang_code=info.get("system_lang_code"),
        )
        # Прокси не выбран — берём наименее загруженный ЖИВОЙ прокси владельца.
        # Иначе каждый добавленный по одному аккаунт молча оставался на общем IP
        # сервера, и флот собирался в одну когорту по самому сильному признаку
        # связи. Стандарт продукта (proxy_selector.audit_proxy_isolation) —
        # один аккаунт на один IP.
        if not pid and acc_id:
            try:
                from services.proxy_balancer import plan_distribution

                prows = await pool.fetch(
                    """SELECT p.id,
                              (SELECT COUNT(*) FROM tg_accounts a
                                WHERE a.proxy_id = p.id) AS used
                         FROM user_proxies p
                        WHERE p.owner_id=$1 AND p.is_active
                          AND COALESCE(p.is_alive, TRUE)""", _uid)
                plan = plan_distribution(
                    [(int(r["id"]), int(r["used"] or 0)) for r in prows], 1)
                pid = plan[0] if plan else None
            except Exception as e:
                log.debug("auto proxy pick failed acc=%s: %s", acc_id, e)
        if pid and acc_id:
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3",
                    pid, acc_id, _uid)
            except Exception as e:
                log.warning("proxy bind failed acc=%s: %s", acc_id, e)
        return acc_id

    def _login_err(exc, fallback: str) -> str:
        """Отказ Telegram → действие, по-русски.

        Общий текст («Не удалось повторно отправить код») — тупик: владелец не
        узнаёт из него ни что ждать, ни что начать заново, а автоповтор на
        фронте останавливается молча. Настоящую причину знает только лог,
        куда владелец не заглядывает.
        """
        try:
            from services import account_manager as am
            return am.login_error_ru(exc) or fallback
        except Exception:
            return fallback

    async def account_add_phone_start(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            body = await request.json()
            phone = normalize_phone(body.get("phone") or "")
            if not phone:
                return _err("Укажите номер телефона (7–15 цифр), например +71234567890", 400)
            _pid, proxy_url = await _resolve_proxy_url(uid, body.get("proxy_id"))
            from services import account_manager as am
            phone_code_hash, hint = await asyncio.wait_for(
                am.start_login(phone, proxy_url=proxy_url, owner_id=uid), timeout=40)
            # Telegram отдаёт код В ПРИЛОЖЕНИЕ — в любую живую сессию номера, а
            # не владельцу в SMS. Если такая сессия есть у нас, код можно
            # прочитать прямо из служебного чата 777000 и не ждать доставки
            # вовсе. Номер в tg_accounts хранится как отдал Telegram (часто без
            # «+»), поэтому сверяем по одним цифрам.
            digits = re.sub(r"\D", "", phone)
            code_acc = await _safe_fetchval(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
                "AND session_str IS NOT NULL AND session_str <> '' "
                "AND regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = $2 "
                "LIMIT 1", uid, digits) if digits else None
            return _json_resp({"ok": True, "phone_code_hash": phone_code_hash,
                               "hint": hint,
                               "code_from_account": int(code_acc) if code_acc else None})
        except asyncio.TimeoutError:
            return _err("Telegram не ответил за 40с — проверьте номер/прокси", 400)
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception as exc:
            log.exception("account_add_phone_start uid=%s", uid)
            return _err(_login_err(exc, "Не удалось отправить код"), 400)

    async def account_add_phone_code(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            body = await request.json()
            # Тот же нормализатор, что и в start — Telethon сверяет вход по номеру,
            # он должен совпадать байт-в-байт с использованным при отправке кода.
            phone = normalize_phone(body.get("phone") or "") or (body.get("phone") or "").strip()
            code = (body.get("code") or "").strip()
            pch = (body.get("phone_code_hash") or "").strip()
            if not phone or not code or not pch:
                return _err("Нужны phone, code и phone_code_hash", 400)
            from services import account_manager as am
            res = await asyncio.wait_for(am.confirm_code(phone, code, pch), timeout=40)
            if res == "need_2fa":
                return _json_resp({"ok": True, "needs_2fa": True})
            session_str, info = await am.get_client_info_and_session(phone)
            pid, _ = await _resolve_proxy_url(uid, body.get("proxy_id"))
            acc_id = await _persist_login(uid, session_str, info, pid)
            return _json_resp({"ok": True, "account_id": acc_id, "username": info.get("username")})
        except asyncio.TimeoutError:
            return _err("Telegram не ответил за 40с", 400)
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception:
            log.exception("account_add_phone_code uid=%s", uid)
            return _err("Не удалось подтвердить код", 400)

    async def account_add_phone_2fa(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            body = await request.json()
            phone = (body.get("phone") or "").strip()
            password = body.get("password") or ""
            if not phone or not password:
                return _err("Нужны phone и password", 400)
            from services import account_manager as am
            await asyncio.wait_for(am.confirm_2fa(phone, password), timeout=40)
            session_str, info = await am.get_client_info_and_session(phone)
            pid, _ = await _resolve_proxy_url(uid, body.get("proxy_id"))
            acc_id = await _persist_login(uid, session_str, info, pid)
            return _json_resp({"ok": True, "account_id": acc_id, "username": info.get("username")})
        except asyncio.TimeoutError:
            return _err("Telegram не ответил за 40с", 400)
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception:
            log.exception("account_add_phone_2fa uid=%s", uid)
            return _err("Не удалось подтвердить 2FA", 400)

    async def account_add_phone_resend(request: web.Request) -> web.Response:
        """Повторный запрос кода авторизации (кнопка «Повторить»).

        Telegram не всегда присылает код с первого раза, поэтому владельцу нужна
        возможность запрашивать его повторно — в т.ч. автоповтором на фронте,
        пока код не придёт. ResendCode просит Telegram прислать код следующим
        способом (обычно SMS, если первым был пуш в приложение).

        FloodWait отдаём СТРУКТУРНО (ok=false, flood=true, retry_after) со
        статусом 200, а не ошибкой: фронт по этому полю показывает, сколько
        ждать, и сам останавливает автоповтор, а не уходит в общий тост ошибки.
        timeout — рекомендованная Telegram пауза до следующего запроса; фронт
        пейсит по ней автоповтор.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            body = await request.json()
            # Тот же нормализатор, что и в start/code — Telethon сверяет номер
            # байт-в-байт с тем, что ушёл в send_code_request.
            phone = normalize_phone(body.get("phone") or "") or (body.get("phone") or "").strip()
            pch = (body.get("phone_code_hash") or "").strip()
            if not phone or not pch:
                return _err("Нужны phone и phone_code_hash", 400)
            from services import account_manager as am
            from telethon.errors import FloodWaitError
            try:
                new_hash, hint, timeout = await asyncio.wait_for(
                    am.resend_code(phone, pch), timeout=40)
            except FloodWaitError as fw:
                secs = int(getattr(fw, "seconds", 0) or 0)
                return _json_resp({
                    "ok": False, "flood": True, "retry_after": secs,
                    "hint": f"⏳ Telegram просит подождать {secs} сек перед новым запросом кода",
                })
            return _json_resp({"ok": True, "phone_code_hash": new_hash,
                               "hint": hint, "timeout": timeout})
        except asyncio.TimeoutError:
            return _err("Telegram не ответил за 40с", 400)
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception as exc:
            log.exception("account_add_phone_resend uid=%s", uid)
            return _err(_login_err(exc, "Не удалось повторно отправить код"), 400)

    async def account_add_qr_start(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            import base64
            from services import account_manager as am
            png = await asyncio.wait_for(am.start_qr_login(uid), timeout=40)
            b64 = base64.b64encode(png).decode("ascii")
            return _json_resp({"ok": True, "qr_png": f"data:image/png;base64,{b64}"})
        except asyncio.TimeoutError:
            return _err("Не удалось создать QR за 40с", 400)
        except Exception:
            log.exception("account_add_qr_start uid=%s", uid)
            return _err("Не удалось создать QR", 400)

    async def account_add_qr_poll(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            body = await request.json()
            from services import account_manager as am
            from telethon.errors import SessionPasswordNeededError
            try:
                # короткий таймаут — фронт опрашивает периодически
                session_str, info = await am.wait_qr_login(uid, timeout=8.0)
            except SessionPasswordNeededError:
                return _json_resp({"ok": True, "status": "need_2fa"})
            except (asyncio.TimeoutError, TimeoutError):
                return _json_resp({"ok": True, "status": "pending"})
            except ValueError as ve:
                # QR-сессия не найдена/истекла — начать заново
                return _err(str(ve), 400)
            pid, _ = await _resolve_proxy_url(uid, body.get("proxy_id"))
            acc_id = await _persist_login(uid, session_str, info, pid)
            return _json_resp({"ok": True, "status": "done", "account_id": acc_id,
                               "username": info.get("username")})
        except Exception:
            log.exception("account_add_qr_poll uid=%s", uid)
            return _err("Ошибка ожидания QR", 400)

    async def account_add_qr_2fa(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            body = await request.json()
            password = body.get("password") or ""
            if not password:
                return _err("Введите пароль 2FA", 400)
            from services import account_manager as am
            session_str, info = await asyncio.wait_for(am.confirm_qr_2fa(uid, password), timeout=40)
            pid, _ = await _resolve_proxy_url(uid, body.get("proxy_id"))
            acc_id = await _persist_login(uid, session_str, info, pid)
            return _json_resp({"ok": True, "account_id": acc_id, "username": info.get("username")})
        except asyncio.TimeoutError:
            return _err("Telegram не ответил за 40с", 400)
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception:
            log.exception("account_add_qr_2fa uid=%s", uid)
            return _err("Не удалось подтвердить 2FA", 400)

    async def _multipart_file_and_proxy(request, max_bytes: int):
        """Достать (file_bytes, filename, proxy_id) из multipart-запроса."""
        reader = await request.multipart()
        file_bytes = b""
        filename = ""
        proxy_id = None
        async for part in reader:
            if part.name == "file":
                filename = part.filename or ""
                file_bytes = await read_upload(part, max_bytes)
            elif part.name == "proxy_id":
                proxy_id = (await read_upload_text(part)).strip() or None
        return file_bytes, filename, proxy_id

    async def account_add_session_file(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            file_bytes, filename, proxy_id = await _multipart_file_and_proxy(request, 20 * 1024 * 1024)
            if not file_bytes:
                return _err("Файл сессии не получен", 400)
            from services import account_manager as am
            session_str, info = await asyncio.wait_for(
                am.import_from_session_file(file_bytes, filename), timeout=60)
            pid, _ = await _resolve_proxy_url(uid, proxy_id)
            acc_id = await _persist_login(uid, session_str, info, pid)
            return _json_resp({"ok": True, "account_id": acc_id, "username": info.get("username")})
        except ValueError as ve:
            return _err(str(ve), 400)
        except asyncio.TimeoutError:
            return _err("Импорт занял слишком долго — проверьте прокси/сессию", 400)
        except Exception:
            log.exception("account_add_session_file uid=%s", uid)
            return _err("Не удалось импортировать .session", 400)

    async def account_add_tdata(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            file_bytes, filename, proxy_id = await _multipart_file_and_proxy(request, 50 * 1024 * 1024)
            if not file_bytes:
                return _err("ZIP-архив не получен", 400)
            if not filename.lower().endswith(".zip"):
                return _err("Ожидается ZIP-архив папки tdata", 400)
            from services import account_manager as am
            # Архив может нести ОДИН или МНОГО аккаунтов (по папке tdata на
            # каждый). Импортируем ВСЕ: битая папка/сессия не рушит партию.
            res = await asyncio.wait_for(
                am.import_tdata_accounts_from_zip_bytes(file_bytes), timeout=240)
            accounts = res.get("accounts") or []
            conv_errors = res.get("errors") or []
            if not accounts:
                _why = "; ".join(conv_errors[:3]) or "аккаунты не найдены"
                return _err(f"Не удалось импортировать ни одного аккаунта: {_why}", 400)
            pid, _ = await _resolve_proxy_url(uid, proxy_id)
            added: list[int] = []
            add_errors: list[str] = []
            for session_str, info in accounts:
                try:
                    # pid=None → _persist_login сам разложит по живым прокси
                    # (один аккаунт на прокси) — это и есть «Авто» со скрина.
                    acc_id = await _persist_login(uid, session_str, info, pid)
                    added.append(acc_id)
                except Exception as _pe:
                    add_errors.append(f"{info.get('phone') or info.get('username') or '—'}: {str(_pe)[:80]}")
            errors = (conv_errors + add_errors)[:8]
            return _json_resp({
                "ok": bool(added),
                "added": len(added),
                "found": res.get("found", len(accounts)),
                "account_id": added[0] if added else None,
                "errors": errors,
            })
        except ValueError as ve:
            return _err(str(ve), 400)
        except ImportError:
            return _err("Конвертер tdata недоступен на сервере — используйте .session или строку", 400)
        except asyncio.TimeoutError:
            return _err("Конвертация tdata заняла слишком долго", 400)
        except Exception:
            log.exception("account_add_tdata uid=%s", uid)
            return _err("Не удалось импортировать tdata", 400)

    app.router.add_post("/api/miniapp/accounts/revive", accounts_revive)
    app.router.add_get("/api/miniapp/accounts", accounts)
    app.router.add_get("/api/miniapp/accounts/export", accounts_export)
    app.router.add_get("/api/miniapp/account/{acc_id}", account_detail)
    app.router.add_post("/api/miniapp/account/{acc_id}/warmup/start", start_warmup)
    app.router.add_post("/api/miniapp/account/{acc_id}/warmup/pause", pause_warmup)
    app.router.add_post("/api/miniapp/accounts/check", accounts_check)
    app.router.add_post("/api/miniapp/accounts/check_all", accounts_check)
    app.router.add_post("/api/miniapp/accounts/mass", accounts_mass)
    app.router.add_post("/api/miniapp/account/{acc_id}/profile", account_profile)
    app.router.add_post("/api/miniapp/account/{acc_id}/login_code", account_login_code)
    app.router.add_post("/api/miniapp/account/{acc_id}/check_restriction", account_check_restriction)
    app.router.add_get("/api/miniapp/accounts/export_json", accounts_export_json)
    app.router.add_post("/api/miniapp/account/{acc_id}/toggle", account_toggle)
    app.router.add_post("/api/miniapp/account/{acc_id}/check", account_check_one)
    app.router.add_post("/api/miniapp/account/{acc_id}/action/{act}", account_action)
    app.router.add_post("/api/miniapp/account/{acc_id}/spamblock_appeal", account_spamblock_appeal)
    app.router.add_get("/api/miniapp/account/{acc_id}/dialogs", account_dialogs)
    app.router.add_get("/api/miniapp/account/{acc_id}/dialog/{peer}/history", account_dialog_history)
    app.router.add_post("/api/miniapp/account/{acc_id}/dialog/{peer}/send", account_dialog_send)
    app.router.add_post("/api/miniapp/account/{acc_id}/dialog/{peer}/send_file", account_dialog_send_file)
    app.router.add_get("/api/miniapp/account/{acc_id}/contacts", account_contacts)
    app.router.add_get("/api/miniapp/account/{acc_id}/channels", account_channels)
    app.router.add_get("/api/miniapp/account/{acc_id}/bots", account_bots)
    app.router.add_post("/api/miniapp/account/{acc_id}/post_story", account_post_story)
    app.router.add_post("/api/miniapp/account/{acc_id}/proxy", account_set_proxy)
    app.router.add_post("/api/miniapp/account/{acc_id}/note", account_set_note)
    app.router.add_post("/api/miniapp/account/{acc_id}/meta", account_set_meta)
    app.router.add_delete("/api/miniapp/account/{acc_id}", account_delete)
    app.router.add_get("/api/miniapp/accounts/health", accounts_health)
    app.router.add_post("/api/miniapp/account/add/phone/start", account_add_phone_start)
    app.router.add_post("/api/miniapp/account/add/phone/code", account_add_phone_code)
    app.router.add_post("/api/miniapp/account/add/phone/2fa", account_add_phone_2fa)
    app.router.add_post("/api/miniapp/account/add/phone/resend", account_add_phone_resend)
    app.router.add_post("/api/miniapp/account/add/qr/start", account_add_qr_start)
    app.router.add_post("/api/miniapp/account/add/qr/poll", account_add_qr_poll)
    app.router.add_post("/api/miniapp/account/add/qr/2fa", account_add_qr_2fa)
    app.router.add_post("/api/miniapp/account/add/session_file", account_add_session_file)
    app.router.add_post("/api/miniapp/account/add/tdata", account_add_tdata)
