"""Приглашения в каналы и группы — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 14 маршрутов `/api/miniapp/invite/*` и
15 функций (включая внутренний помощник `_own_channel_acc_ids`, которым
пользуются только эти ручки). Приглашения — самая баноопасная операция
продукта, и её разбор запроса жил в файле на 22 тысячи строк вперемешку с
контактами, флотом и облаком.

Здесь — только разбор запроса, скоуп владельца, русские ошибки и постановка
операции через `operation_bus`; проверка прав аккаунтов, оценка аудитории и
сама отправка живут в `services/*`. Поведение при переносе не менялось: тела
обработчиков и строки регистрации маршрутов перенесены дословно.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты приглашений на приложении мини-аппа."""
    from services import account_status as _acc_status
    from services import operation_bus as _obus
    from services.mini_app_api import (
        _INTERNAL_ERROR, _PREFLIGHT_CONCURRENCY, _err, _get_uid, _json_resp,
        _safe_count, _safe_fetch, _safe_fetchrow, _scan_admin_status, log,
        read_upload, read_upload_text, UploadTooLarge,
    )

    async def invite_retention_overview(request: web.Request) -> web.Response:
        """Ретеншен инвайта: приток (успешные вступления) vs отток (left-события).

        joined — из operation_log по инвайт-операциям владельца за период;
        left — из organism_events kind='left' (chat_guard). Свёртка — чистый
        invite_retention.summarize. Отток виден только по модерируемым чатам."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.query.get("days", "30"))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 180))
        joined = await _safe_count(pool,
            """SELECT COUNT(*) FROM operation_log ol
               JOIN operation_queue oq ON oq.id = ol.op_id
               WHERE oq.owner_id=$1 AND oq.op_type LIKE '%invite%'
                 AND ol.status='ok' AND ol.message='joined'
                 AND ol.created_at > NOW() - ($2 || ' days')::interval""",
            uid, str(days))
        left = await _safe_count(pool,
            "SELECT COUNT(*) FROM organism_events WHERE owner_id=$1 AND kind='left' "
            "AND created_at > NOW() - ($2 || ' days')::interval", uid, str(days))
        from services import invite_retention
        summary = invite_retention.summarize(joined, left)
        summary["health"] = invite_retention.health(summary["retention_pct"])
        summary["days"] = days
        # Где именно уходят. Одна общая цифра оттока не говорит, что делать:
        # приветствие и правила настраиваются по конкретному чату.
        summary["by_chat"] = [
            {"chat_id": str(r["chat_id"] or ""), "title": str(r["title"] or ""),
             "left": int(r["c"] or 0)}
            for r in await _safe_fetch(
                pool,
                "SELECT payload->>'chat_id' AS chat_id, "
                "       COALESCE(MAX(payload->>'title'), '') AS title, "
                "       COUNT(*) AS c "
                "FROM organism_events "
                "WHERE owner_id=$1 AND kind='left' "
                "  AND created_at > NOW() - ($2 || ' days')::interval "
                "GROUP BY payload->>'chat_id' ORDER BY c DESC LIMIT 20",
                uid, str(days))
        ]
        return _json_resp(summary)

    async def invite_analytics(request: web.Request) -> web.Response:
        """Аналитика инвайтинга по флоту: факты за сегодня и за 7 дней.

        Раньше пользователь видел только итог одной операции («добавлено 40/50»)
        и не мог ответить на главные вопросы: растёт ли риск, какие аккаунты
        тянут флот вниз, можно ли ускоряться. Здесь всё считается из
        `account_daily_stats` — тех самых суточных фактов, которые до этого
        никто не писал.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)

        today = await _safe_fetchrow(
            pool,
            """SELECT COALESCE(SUM(s.invites_ok), 0)   AS ok,
                      COALESCE(SUM(s.actions_fail), 0) AS failed,
                      COALESCE(SUM(s.flood_events), 0) AS floods,
                      COUNT(DISTINCT s.account_id)     AS accounts
               FROM account_daily_stats s
               JOIN tg_accounts a ON a.id = s.account_id
               WHERE a.owner_id = $1 AND s.stat_date = CURRENT_DATE""",
            uid,
        ) or {}
        # ПЛОТНЫЙ ряд из 7 дней: generate_series даёт по строке на каждый из
        # последних 7 дней, дни без активности → 0. Раньше GROUP BY по факту
        # возвращал ТОЛЬКО дни с данными, и график в мини-аппе из 1–2 столбцов
        # растягивал их на пол-ширины («кривой» график). Теперь всегда 7 столбцов.
        week = await _safe_fetch(
            pool,
            """SELECT to_char(d.day, 'YYYY-MM-DD')          AS day,
                      COALESCE(SUM(s.invites_ok), 0)         AS ok,
                      COALESCE(SUM(s.flood_events), 0)       AS floods
               FROM generate_series(
                        (CURRENT_DATE - INTERVAL '6 days')::date,
                        CURRENT_DATE,
                        INTERVAL '1 day') AS d(day)
               LEFT JOIN (
                   SELECT s.stat_date, s.invites_ok, s.flood_events
                   FROM account_daily_stats s
                   JOIN tg_accounts a ON a.id = s.account_id
                   WHERE a.owner_id = $1
               ) s ON s.stat_date = d.day::date
               GROUP BY d.day ORDER BY d.day""",
            uid,
        )
        # Лучший/худший за неделю: лучший — больше всего успешных при нуле флудов,
        # худший — больше всего флудов. Показываем ФАКТ, а не «оценку».
        ranked = await _safe_fetch(
            pool,
            """SELECT s.account_id,
                      COALESCE(a.phone, a.first_name, '#' || a.id::text) AS label,
                      COALESCE(SUM(s.invites_ok), 0)   AS ok,
                      COALESCE(SUM(s.actions_fail), 0) AS failed,
                      COALESCE(SUM(s.flood_events), 0) AS floods
               FROM account_daily_stats s
               JOIN tg_accounts a ON a.id = s.account_id
               WHERE a.owner_id = $1 AND s.stat_date >= CURRENT_DATE - INTERVAL '6 days'
               GROUP BY s.account_id, a.phone, a.first_name, a.id
               ORDER BY floods DESC, ok DESC""",
            uid,
        ) or []

        ok_t = int((today or {}).get("ok") or 0)
        fail_t = int((today or {}).get("failed") or 0)
        total_t = ok_t + fail_t
        best = min(ranked, key=lambda r: (int(r["floods"]), -int(r["ok"])), default=None)
        worst = max(ranked, key=lambda r: (int(r["floods"]), int(r["failed"])), default=None)

        return _json_resp({
            "today": {
                "ok": ok_t,
                "failed": fail_t,
                "floods": int((today or {}).get("floods") or 0),
                "accounts": int((today or {}).get("accounts") or 0),
                # Конверсия честная: доля успеха среди ПОПЫТОК, а не «сколько
                # запросили». Нет попыток — показываем null, а не 0%.
                "conversion_pct": round(ok_t / total_t * 100, 1) if total_t else None,
            },
            "week": [dict(r) for r in (week or [])],
            "best": dict(best) if best else None,
            "worst": dict(worst) if worst else None,
            "has_data": bool(ranked),
        })

    async def invite_parse_list(request: web.Request) -> web.Response:
        """Предпросмотр «Своего списка» ДО запуска: сколько распознано и что нет.

        Раньше пользователь узнавал результат разбора только по факту операции
        («Добавлено 0/0»). Теперь мини-апп показывает сразу: N номеров + M
        @username/ID распознано, K строк не понято — и какие именно.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        raw = str(body.get("import_list") or "")
        from services.mass_inviter_engine import classify_invite_list
        r = classify_invite_list(raw)
        total = len(r["phones"]) + len(r["user_refs"])
        # Превью дедупа (паритет с ботом): сколько из списка уже приглашались в
        # эту группу. Группа опциональна — без неё дедуп не считаем.
        already = 0
        _grp = str(body.get("group") or "").strip()
        if _grp:
            from services.invite_preflight import count_already_invited
            already = await count_already_invited(
                pool, uid, _grp, list(r["phones"]) + list(r["user_refs"]))
        return _json_resp({
            "phones": len(r["phones"]),
            "user_refs": len(r["user_refs"]),
            "total": total,
            "already_invited": already,
            "new": max(0, total - already),
            "unrecognized": r["unrecognized"],
            "unrecognized_count": len(r["unrecognized"]),
        })

    async def invite_parse_file(request: web.Request) -> web.Response:
        """Загрузка списка ФАЙЛОМ (паритет с ботом): .txt/.csv/.xlsx/.db → разбор
        в user_refs/phones. Опционально group → превью дедупа. Multipart upload.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services.invite_list_parser import extract_text, MAX_FILE_BYTES
        try:
            reader = await request.multipart()
        except Exception:
            return _err("Ожидался файл (multipart/form-data)", 400)
        filename = "list.txt"
        data = b""
        group = ""
        too_big = f"Файл слишком большой (>{MAX_FILE_BYTES // (1024 * 1024)} МБ)"
        try:
            async for field in reader:
                if field.name == "file":
                    filename = field.filename or "list.txt"
                    # Предел действует НА ЧТЕНИИ: `field.read()` втянул бы поле
                    # целиком, и проверка длины после этого уже ничего не
                    # защищает — память израсходована.
                    data = await read_upload(field, MAX_FILE_BYTES)
                elif field.name == "group":
                    group = (await read_upload_text(field)).strip()
        except UploadTooLarge:
            return _err(too_big, 413)
        except Exception:
            return _err("Не удалось прочитать загрузку", 400)
        if not data:
            return _err("Файл пуст", 400)
        try:
            text = extract_text(filename, data)
        except Exception:
            return _err("Не удалось прочитать файл (формат не поддержан или повреждён)", 400)
        from services.mass_inviter_engine import split_invite_targets
        user_refs, phones = split_invite_targets(text)
        total = len(user_refs) + len(phones)
        if total == 0:
            return _err("В файле не распознано ни одной цели", 400)
        already = 0
        if group:
            from services.invite_preflight import count_already_invited
            already = await count_already_invited(pool, uid, group, list(phones) + list(user_refs))
        return _json_resp({
            "ok": True, "user_refs": user_refs, "phones": phones, "total": total,
            "already_invited": already, "new": max(0, total - already),
        })

    async def invite_reserve_options(request: web.Request) -> web.Response:
        """Каналы для резерва перелива: кого исполнитель возьмёт, а кого нет и почему."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import invite_overflow as _ovf
        from services.mass_inviter_engine import parse_group_ref
        group = (request.query.get("group") or "").strip()
        _ck = parse_group_ref(group) if group else ""
        try:
            chans = await _ovf.reserve_options(pool, uid, _ck)
            # Цепочка этой кампании: по каким каналам она уже шла, сколько в
            # каждый пригласили и с какого продолжит следующий запуск.
            chain = await _ovf.chain_rows(pool, uid, _ck) if _ck else []
        except Exception:
            log.warning("invite_reserve_options uid=%s", uid, exc_info=True)
            return _err("Не удалось загрузить каналы", 500)
        return _json_resp({"channels": chans, "chain": chain})

    async def invite_audience_size(request: web.Request) -> web.Response:
        """Сколько целей даст выбранный источник — ДО запуска операции.

        Без этого сценарий обрывался молча: пользователь выбирал источник (по
        умолчанию «из парсера»), жал «Запустить», операция уходила в очередь и
        падала с «Аудитория пуста» — а увидеть это можно было только в истории
        операций. Снаружи это выглядит как «инвайтинг не работает совсем».
        DM-кампании считают получателей до старта (`total_targets`) — инвайт нет.

        Запросы намеренно повторяют те, что делает `op_worker._exec_mass_invite`
        при загрузке аудитории: показанное число обязано совпадать с тем, что
        реально возьмёт исполнитель. Расхождение здесь хуже отсутствия счётчика —
        оно врёт с видом точности. Сторожит tests/test_invite_audience_size.py.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        source = (request.query.get("source") or "parsed").strip()
        if source not in ("parsed", "crm", "bot_users", "segment"):
            return _err("Неизвестный источник аудитории", 400)

        total, hint = 0, ""
        try:
            if source == "parsed":
                _pr = request.query.get("parse_run_id")
                try:
                    _pr = int(_pr) if _pr else None
                except (TypeError, ValueError):
                    _pr = None
                if _pr:
                    total = await _safe_count(
                        pool,
                        "SELECT COUNT(*) FROM parsed_audiences "
                        "WHERE owner_id=$1 AND parse_run_id=$2 "
                        "AND (username IS NOT NULL OR tg_user_id IS NOT NULL)",
                        uid, _pr)
                else:
                    total = await _safe_count(
                        pool,
                        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 "
                        "AND (username IS NOT NULL OR tg_user_id IS NOT NULL)",
                        uid)
                hint = "Соберите аудиторию в «Парсер аудитории» — оттуда она попадёт сюда."
            elif source == "crm":
                total = await _safe_count(
                    pool,
                    "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1 "
                    "AND (username IS NOT NULL OR tg_user_id IS NOT NULL OR phone IS NOT NULL)",
                    uid)
                hint = "Добавьте контакты в CRM или импортируйте их."
            elif source == "segment":
                from services.contacts_hub import repository as _crepo
                _ssid = request.query.get("saved_segment_id")
                _filters = None
                if _ssid and str(_ssid).isdigit():
                    _filters = await _crepo.get_segment_filters(pool, uid, int(_ssid))
                # Быстрый выбор (избранные / по тегу) шлёт segment_filters —
                # превью обязано считать ИМЕННО их, иначе счётчик покажет «весь
                # список», а операция возьмёт срез: число соврёт с видом точности.
                if _filters is None:
                    _sf_raw = request.query.get("segment_filters")
                    if _sf_raw:
                        try:
                            _filters = _crepo._clean_filters(json.loads(_sf_raw))
                        except Exception:
                            _filters = None
                total = await _crepo.count_segment(pool, uid, _filters or {})
                hint = "Сохраните срез контактов («Сегменты») — приглашайте его напрямую."
            else:
                total = await _safe_count(
                    pool,
                    "SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu "
                    "JOIN managed_bots mb ON mb.bot_id = bu.bot_id "
                    "WHERE mb.added_by=$1 AND bu.is_active=TRUE",
                    uid)
                hint = "Подписчики появятся, когда люди начнут писать вашим ботам."
        except Exception:
            log.warning("invite_audience_size uid=%s source=%s", uid, source, exc_info=True)
            # Честно говорим «не знаем», а не показываем 0: ноль здесь означал бы
            # «источник пуст» и оттолкнул бы от запуска рабочей операции.
            return _json_resp({"source": source, "total": None, "hint": ""})

        # Сколько из этого реально пойдёт в прогон: фильтры аудитории, уже
        # приглашённые в этот канал, «не приглашать», повторы — теми же
        # ключами, что у исполнителя. Без этого вторая кампания в тот же канал
        # обещала тысячи целей, а прогон заканчивался словами «новых нет».
        breakdown = None
        if source in ("parsed", "crm", "bot_users"):
            from services.invite_preflight import audience_breakdown
            _af = {}
            try:
                _af_raw = request.query.get("aud_filters")
                _af = json.loads(_af_raw) if _af_raw else {}
                if not isinstance(_af, dict):
                    _af = {}
            except Exception:
                _af = {}
            _pr_b = request.query.get("parse_run_id") if source == "parsed" else None
            breakdown = await audience_breakdown(
                pool, uid, source, (request.query.get("group") or "").strip(),
                parse_run_id=int(_pr_b) if _pr_b and str(_pr_b).isdigit() else None,
                aud_filters=_af)
            if breakdown is not None:
                total = breakdown["rows"]

        # Потолок пула целей за прогон — тот же, что у исполнителя (остаток уходит
        # в продолжение). Раньше здесь стояло 2000, когда исполнитель брал уже
        # 20 000, и экран пугал «за прогон возьмём 2 000».
        from services.op_worker import _INVITE_AUDIENCE_CAP as _INV_AUD_CAP
        return _json_resp({
            "source": source,
            "total": int(total or 0),
            "capped_at": _INV_AUD_CAP,
            "hint": hint if not total else "",
            "breakdown": breakdown,
        })

    async def invite_segment_options(request: web.Request) -> web.Response:
        """Быстрые срезы хранилища контактов для инвайта: избранные + топ-тегов.

        Чтобы приглашать из хранилища не только «весь список» и сохранённые
        сегменты, но и «избранные» / «по тегу» без предварительного создания
        сегмента. Счётчики считаются тем же движком сегментов (count_segment),
        что и превью с исполнителем, — показанное число обязано совпасть с тем,
        что реально возьмёт операция.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.contacts_hub import repository as _crepo
            all_n = await _crepo.count_segment(pool, uid, {})
            fav_n = await _crepo.count_segment(pool, uid, {"favorite_only": True})
            tag_rows = await _safe_fetch(
                pool,
                "SELECT t AS tag, COUNT(*) AS cnt FROM unified_contacts, unnest(tags) AS t "
                "WHERE owner_id=$1 GROUP BY t ORDER BY cnt DESC, t LIMIT 8",
                uid)
            tags = [{"tag": r["tag"], "count": int(r["cnt"])} for r in (tag_rows or [])]
            return _json_resp({"all": int(all_n or 0), "favorites": int(fav_n or 0),
                               "tags": tags})
        except Exception:
            log.warning("invite_segment_options uid=%s", uid, exc_info=True)
            return _json_resp({"all": 0, "favorites": 0, "tags": []})

    async def invite_preflight(request: web.Request) -> web.Response:
        """Предполётная проверка инвайта ДО запуска: флот, прокси, оценка проходов.

        Отвечает на «почему за один проход не уходит» и «работают ли аккаунты без
        прокси» ЧЕСТНО, ещё до старта: сколько аккаунтов реально пригодны, сколько
        без прокси (работают напрямую — риск), сколько заблокированы (назначен
        прокси, но он недоступен — прямое соединение убьёт сессию), суточный
        суммарный лимит флота и во сколько проходов уйдёт аудитория.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            audience = int(request.query.get("audience") or 0)
        except (TypeError, ValueError):
            audience = 0
        # В режиме «один проход» оценка ёмкости считается по потолку (до
        # _INVITE_LIMIT_CEILING на аккаунт), а не по консервативному предсказанию.
        one_pass = str(request.query.get("one_pass") or "").lower() in ("1", "true", "yes", "on")
        try:
            # Разбивка флота по транспорту. proxy_broken = назначен прокси, но он
            # неактивен/удалён → напрямую подключать НЕЛЬЗЯ (AUTH_KEY_DUPLICATED),
            # такие аккаунты в инвайт не идут. direct = прокси не назначен вовсе →
            # каноничное прямое соединение, работает, но с риском (предупреждаем).
            rows = await _safe_fetch(pool,
                """SELECT a.id,
                          (a.proxy_id IS NOT NULL AND p.id IS NOT NULL) AS has_proxy,
                          (a.proxy_id IS NOT NULL AND p.id IS NULL)     AS proxy_broken
                   FROM tg_accounts a
                   LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
                   WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL
                     AND """ + _acc_status.sql_not_dead("a.acc_status") + """""",
                uid) or []
            total = len(rows)
            with_proxy = sum(1 for r in rows if r["has_proxy"])
            proxy_broken = sum(1 for r in rows if r["proxy_broken"])
            direct = total - with_proxy - proxy_broken
            # Пригодны = всё, кроме заблокированных по битому прокси. Аккаунты без
            # прокси РАЗРЕШЕНЫ (работают напрямую) — но помечаем предупреждением.
            usable_rows = [r for r in rows if not r["proxy_broken"]]

            # Суммарный суточный бюджет флота (с учётом уже потраченного сегодня и
            # выученных лимитов на аккаунт) + карантин. Это и есть «сколько уйдёт за
            # один проход».
            from services.flood_engine import recommended_daily_limit, _INVITE_LIMIT_CEILING
            from services import infra_memory as _im

            # Карантин всего флота — ОДНИМ запросом до оценки: он читает одну
            # таблицу по списку id, и дробить его на запрос на аккаунт было
            # нечем оправдать. Остаток лимита посчитать так нельзя (на аккаунт
            # он свой), поэтому он остаётся запросом на аккаунт — управляемо
            # параллельно, числом одновременных заметно ниже размера пула,
            # чтобы оценка не выедала соединения у остальных запросов.
            _quarantined_ids = await _im.quarantined_accounts(
                pool, [r["id"] for r in usable_rows[:200]])
            _preflight_sem = asyncio.Semaphore(_PREFLIGHT_CONCURRENCY)

            async def _capacity(r) -> tuple[int, int]:
                """(вклад в ёмкость, 1 если аккаунт в карантине)."""
                async with _preflight_sem:
                    try:
                        if int(r["id"]) in _quarantined_ids:
                            return 0, 1
                        if one_pass:
                            # ёмкость одного прохода = потолок на аккаунт
                            return _INVITE_LIMIT_CEILING, 0
                        _rec = await recommended_daily_limit(pool, int(r["id"]))
                        return int(_rec.get("remaining") or 0), 0
                    except Exception:
                        log.debug("invite_preflight: ёмкость аккаунта %s не посчиталась",
                                  r.get("id"), exc_info=True)
                        return (_INVITE_LIMIT_CEILING if one_pass else 15), 0

            # Потолок в 200 — на случай огромного флота.
            parts = await asyncio.gather(*(_capacity(r) for r in usable_rows[:200]))
            per_pass = sum(p for p, _q in parts)
            quarantined = sum(q for _p, q in parts)
            import math as _math
            est_passes = (_math.ceil(audience / per_pass) if audience and per_pass else
                          (1 if audience and audience <= per_pass else 0))

            warnings = []
            if proxy_broken:
                warnings.append(
                    f"🚫 {proxy_broken} аккаунт(ов) с назначенным, но недоступным прокси — "
                    "в инвайт не пойдут (прямое подключение убьёт сессию, AUTH_KEY_DUPLICATED). "
                    "Почините прокси или снимите привязку.")
            if direct:
                warnings.append(
                    f"⚠️ {direct} аккаунт(ов) без прокси — работают напрямую (разрешено), "
                    "но выше риск бана и AUTH_KEY_DUPLICATED, если та же сессия активна "
                    "где-то ещё (телефон/другой софт). Для массового инвайта надёжнее прокси.")
            if quarantined:
                warnings.append(
                    f"🚫 {quarantined} аккаунт(ов) под риск-пульсом — временно исключены "
                    "(недавнее ограничение). Дайте им отдохнуть.")
            if audience and est_passes and est_passes > 1:
                warnings.append(
                    f"🔁 Аудитория {audience} при суточном лимите флота ~{per_pass} уйдёт за "
                    f"~{est_passes} прохода(ов) (по дню на проход). Один проход — это "
                    "аккаунты × безопасный суточный лимит; лимит растёт по мере прогрева. "
                    "Больше аккаунтов = меньше проходов.")

            usable = len(usable_rows) - quarantined
            return _json_resp({
                "accounts": {
                    "total": total, "with_proxy": with_proxy,
                    "direct": direct, "proxy_broken": proxy_broken,
                    "quarantined": quarantined,
                },
                "usable": max(0, usable),
                "audience": audience,
                "per_pass": per_pass,
                "est_passes": est_passes,
                "one_pass": bool(audience and est_passes <= 1 and usable > 0),
                "can_launch": usable > 0,
                "warnings": warnings,
            })
        except Exception:
            log.exception("invite_preflight uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _own_channel_acc_ids(uid: int, group: str) -> list[int]:
        """Аккаунты, к которым этот канал привязан в «Моих каналах».

        Канал узнаём по id (голый или -100…) или по @username/ссылке."""
        from services.mass_inviter_engine import _as_channel_id
        cid = _as_channel_id(group)
        uname = ""
        if cid is None:
            g = group.strip()
            for pre in ("https://", "http://", "t.me/", "telegram.me/", "@"):
                if g.lower().startswith(pre):
                    g = g[len(pre):]
            g = g.split("/")[0].split("?")[0]
            if g and not g.startswith("+") and g.lower() != "joinchat":
                uname = g
        if cid is None and not uname:
            return []
        rows = await _safe_fetch(pool,
            "SELECT acc_id FROM managed_channels WHERE owner_id=$1 AND acc_id IS NOT NULL "
            "AND (channel_id=$2 OR (LOWER(username)=LOWER($3) AND $3<>''))",
            uid, int(cid or 0), uname)
        return [int(r["acc_id"]) for r in rows or [] if r.get("acc_id")]

    async def invite_rights_check(request: web.Request) -> web.Response:
        """Живая проверка прав ДО запуска: есть ли среди аккаунтов админ ЦЕЛЕВОГО
        чата, способный раздать право приглашать остальным.

        Главная причина «вступили, но не инвайтят»: инвайт в чат/канал требует
        права invite_users, а раздаёт его только аккаунт-создатель/админ с
        «Назначать администраторов». Раньше это выяснялось только по факту
        провала. Здесь — до старта: подключаем несколько аккаунтов к чату и
        смотрим их права (channel_admin_status). Дорогой живой вызов → отдельный
        on-demand эндпоинт, не на горячем пути.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        group = (request.query.get("group") or "").strip()
        if not group:
            return _err("Укажите группу/канал", 400)
        try:
            rows = await _safe_fetch(pool,
                "SELECT a.id, a.owner_id, a.session_str, a.device_model, a.system_version, "
                "a.app_version, a.lang_code, a.system_lang_code, a.proxy_id, a.cf_relay_url, "
                "a.phone, COALESCE(p.proxy_url, NULL) AS proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL "
                "AND " + _acc_status.sql_not_dead("a.acc_status") + " "
                "AND NOT (a.proxy_id IS NOT NULL AND p.id IS NULL) "
                # Сначала аккаунты, к которым канал привязан в «Моих каналах»:
                # создатель канала — и есть промоутер. Раньше брались первые
                # восемь по id, и если владельца среди них не было, а остальные
                # ещё не вступили, экран говорил «ни один аккаунт не админ».
                "ORDER BY (a.id = ANY($2::bigint[])) DESC, a.id LIMIT 8",
                uid, await _own_channel_acc_ids(uid, group)) or []
            if not rows:
                return _json_resp({"has_promoter": False, "has_inviter": False,
                                   "checked": 0, "usable": 0, "promoter_label": None})
            has_promoter = has_inviter = False
            promoter_label = None
            checked = 0
            for r, st in zip(rows, await _scan_admin_status(rows, group)):
                if not st.get("ok"):
                    continue
                checked += 1
                if st.get("can_invite") or st.get("creator"):
                    has_inviter = True
                if st.get("can_promote") and not has_promoter:
                    # Достаточно одного — он раздаст право остальным. Берём
                    # первого по порядку строк, как и при обходе по очереди.
                    has_promoter = True
                    promoter_label = str(r["phone"] or f"#{r['id']}")
            return _json_resp({
                "has_promoter": has_promoter,
                "has_inviter": has_inviter,
                "promoter_label": promoter_label,
                "checked": checked,
                "usable": len(rows),
            })
        except Exception:
            log.exception("invite_rights_check uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def invite_fleet_readiness(request: web.Request) -> web.Response:
        """Паритет с ботом (пре-флайт): по целевой группе показать готовность
        флота — кто подключится, кто в группе, кто админ. Живой вызов (подключает
        аккаунты), поэтому on-demand, не на горячем пути.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        group = (request.query.get("group") or "").strip()
        if not group:
            return _err("Укажите группу/канал", 400)
        try:
            from services.invite_preflight import run_preflight
            _ids = request.query.get("account_ids") or ""
            _acc = [int(x) for x in _ids.split(",") if x.strip().isdigit()] or None
            # Выборка: проверяем до 16 аккаунтов вживую, чтобы уложиться в таймаут
            # шлюза (полный флот проверяется по факту при самой операции).
            rep = await run_preflight(pool, uid, group, account_ids=_acc, limit=16)
            return _json_resp(rep)
        except Exception:
            log.exception("invite_fleet_readiness uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def invite_join_all(request: web.Request) -> web.Response:
        """Паритет с ботом: вступить всеми аккаунтами в группу (для прямого
        инвайта нужно членство).

        Живое вступление флотом — долгая операция (коннект+join на каждый
        аккаунт), поэтому НЕ выполняем инлайн в запросе (шлюз рвёт по таймауту),
        а ставим фоновую операцию bulk_join (один чат × N аккаунтов) и сразу
        отдаём op_id. Прогресс/отмена — через общий трекер операций.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        group = (body.get("group") or request.query.get("group") or "").strip()
        if not group:
            return _err("Укажите группу/канал", 400)
        # Аккаунты — только свои и живые (иначе операция провалится в воркере).
        _ids = [int(x) for x in (body.get("account_ids") or []) if str(x).isdigit()]
        if _ids:
            rows = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id=ANY($2::bigint[]) "
                "AND is_active AND session_str IS NOT NULL", uid, _ids)
        else:
            rows = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
                "AND session_str IS NOT NULL ORDER BY trust_score DESC NULLS LAST LIMIT 200", uid)
        acc_ids = [int(r["id"]) for r in (rows or [])]
        if not acc_ids:
            return _err("Нет доступных активных аккаунтов", 400)
        # Ban-safety: массовый join — рисковая операция; бот проверяет давление
        # инфраструктуры ПЕРЕД постановкой, мини-апп обязан так же.
        try:
            from services import infra_orchestrator
            ready, reason = await infra_orchestrator.is_ready_for_op(pool, uid, "bulk_join")
            if not ready:
                return _err(f"Инфраструктура перегружена: {reason}", 429)
        except Exception:
            log.warning("invite_join_all: pressure check failed", exc_info=True)
        params = {"links": [group], "account_ids": acc_ids, "delay_mode": "smart"}
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "bulk_join", params,
                total_items=len(acc_ids),
                label=f"Вступление в {group} × {len(acc_ids)} акк.",
            )
            return _json_resp({"ok": True, "op_id": op_id, "accounts": len(acc_ids)})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("invite_join_all uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def invite_grant_admin(request: web.Request) -> web.Response:
        """В один тап: найти аккаунт-админа чата и через него выдать ОСТАЛЬНЫМ
        аккаунтам право приглашать (операция promote_all_admins).

        Убирает ручной шаг «сделай каждый аккаунт админом в Telegram». Промоутер
        подбирается автоматически (создатель/админ с add_admins). Идёт как
        Operation через operation_bus (прогресс/отмена/гейт по тарифу), а не инлайн.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        group = (str(body.get("group") or "").strip()
                 or (request.query.get("group") or "").strip())
        if not group:
            return _err("Укажите группу/канал", 400)
        try:
            rows = await _safe_fetch(pool,
                "SELECT a.id, a.owner_id, a.session_str, a.device_model, a.system_version, "
                "a.app_version, a.lang_code, a.system_lang_code, a.proxy_id, a.cf_relay_url, "
                "a.phone, COALESCE(p.proxy_url, NULL) AS proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL "
                "AND " + _acc_status.sql_not_dead("a.acc_status") + " "
                "AND NOT (a.proxy_id IS NOT NULL AND p.id IS NULL) "
                # Владелец канала из «Моих каналов» — первым (см. rights_check).
                "ORDER BY (a.id = ANY($2::bigint[])) DESC, a.id LIMIT 8",
                uid, await _own_channel_acc_ids(uid, group)) or []
            if not rows:
                return _err("Нет доступных аккаунтов", 404)
            promoter = None
            channel_id = None
            for r, st in zip(rows, await _scan_admin_status(rows, group)):
                if st.get("ok") and st.get("can_promote") and st.get("channel_id"):
                    promoter = r
                    channel_id = int(st["channel_id"])
                    break
            if promoter is None:
                return _json_resp({
                    "ok": False, "no_promoter": True,
                    "message": ("Ни один аккаунт не админ этого чата с правом «Назначать "
                                "администраторов». Сделайте один свой аккаунт таким админом "
                                "в Telegram — тогда я раздам право приглашать остальным."),
                })
            op_id = await _obus.submit(
                pool, uid, "promote_all_admins",
                {"channel_id": channel_id, "owner_acc_id": int(promoter["id"])},
                total_items=1, label="Выдача прав инвайтерам")
            return _json_resp({
                "ok": True, "op_id": op_id,
                "promoter_label": str(promoter["phone"] or f"#{promoter['id']}"),
            })
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("invite_grant_admin uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def invite_advice(request: web.Request) -> web.Response:
        """Аналитик инвайтинга: не «что произошло», а «что теперь делать».

        Аналитика показывает цифры; они не подсказывают, что три конкретных
        аккаунта пора вывести из ротации, что у половины флота пустой профиль и
        он поэтому сам себе режет лимит, или что аккаунтов не хватит под
        заявленную аудиторию. Разбор детерминированный, по БД: каждая
        рекомендация несёт числа, на которых построена, и её можно перепроверить.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            audience = max(0, int(request.query.get("audience") or 0))
        except (TypeError, ValueError):
            audience = 0
        try:
            from services import invite_advisor
            data = await invite_advisor.build_advice(pool, uid, audience_size=audience)
        except Exception:
            log.warning("invite_advice failed uid=%s", uid, exc_info=True)
            return _err("Не удалось собрать разбор", 500)
        return _json_resp(data)

    async def invite_account_card(request: web.Request) -> web.Response:
        """Карточка аккаунта для инвайтинга: лимит, пауза, риск — с объяснением."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор аккаунта", 400)

        acc = await _safe_fetchrow(
            pool,
            # В tg_accounts колонка называется added_at; created_at нет, и запрос
            # падал — _safe_fetchrow глотал ошибку, и карточка существующего
            # аккаунта отвечала «Аккаунт не найден». Имя поля в ответе сохраняем.
            "SELECT id, phone, first_name, is_active, trust_score, "
            "added_at AS created_at, cooldown_until "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            acc_id, uid,
        )
        if not acc:
            return _err("Аккаунт не найден", 404)

        limit_info, pause_s, risk = {}, None, {}
        try:
            from services import flood_engine as _fe
            limit_info = await _fe.recommended_daily_limit(pool, acc_id)
            pause_s = round(_fe.recommended_delay(acc_id, "invite"), 1)
            risk = _fe.get_risk_summary([acc_id]).get(acc_id, {})
        except Exception:
            log.warning("invite_account_card: engine unavailable acc=%s", acc_id, exc_info=True)

        week = await _safe_fetchrow(
            pool,
            """SELECT COALESCE(SUM(invites_ok), 0)   AS ok,
                      COALESCE(SUM(actions_fail), 0) AS failed,
                      COALESCE(SUM(flood_events), 0) AS floods
               FROM account_daily_stats
               WHERE account_id=$1 AND stat_date >= CURRENT_DATE - INTERVAL '6 days'""",
            acc_id,
        ) or {}

        age_days = None
        if acc.get("created_at"):
            try:
                import datetime as _d
                age_days = (_d.datetime.now(_d.timezone.utc) - acc["created_at"]).days
            except Exception:
                age_days = None

        return _json_resp({
            "id": acc["id"],
            "label": acc.get("phone") or acc.get("first_name") or f"#{acc['id']}",
            "age_days": age_days,
            "trust_score": acc.get("trust_score"),
            "recommended_limit": limit_info.get("limit"),
            "used_today": limit_info.get("used_today"),
            "remaining_today": limit_info.get("remaining"),
            "limit_basis": limit_info.get("basis"),
            # Факторы отдаём отдельным полем: пользователь должен видеть не только
            # ЧТО система решила, но и по каким признакам аккаунта.
            "limit_factors": limit_info.get("factors") or {},
            "recommended_pause_s": pause_s,
            "risk": risk,
            "week": {k: int(week.get(k) or 0) for k in ("ok", "failed", "floods")},
        })

    app.router.add_get("/api/miniapp/invite/analytics", invite_analytics)
    app.router.add_get("/api/miniapp/invite/audience", invite_audience_size)
    app.router.add_get("/api/miniapp/invite/reserve_options", invite_reserve_options)
    app.router.add_get("/api/miniapp/invite/segment_options", invite_segment_options)
    app.router.add_post("/api/miniapp/invite/parse_list", invite_parse_list)
    app.router.add_post("/api/miniapp/invite/parse_file", invite_parse_file)
    app.router.add_get("/api/miniapp/invite/preflight", invite_preflight)
    app.router.add_get("/api/miniapp/invite/rights_check", invite_rights_check)
    app.router.add_get("/api/miniapp/invite/fleet_readiness", invite_fleet_readiness)
    app.router.add_post("/api/miniapp/invite/join_all", invite_join_all)
    app.router.add_post("/api/miniapp/invite/grant_admin", invite_grant_admin)
    app.router.add_get("/api/miniapp/invite/advice", invite_advice)
    app.router.add_get("/api/miniapp/invite/account/{acc_id}", invite_account_card)
    app.router.add_get("/api/miniapp/invite/retention", invite_retention_overview)
