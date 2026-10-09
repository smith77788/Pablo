"""Прокси владельца: пул, проверка, ротация, переезд аккаунтов, выгрузка.

Вынесено из `services/mini_app_api.py`: тот файл — больше 25 тысяч строк в
одной функции `setup_routes`, и четырнадцать обработчиков прокси лежали в нём
ТРЕМЯ кусками — основной блок в середине, ротация на девять тысяч строк выше,
статистика на пять тысяч ниже. Группа собрана в один модуль не для красоты:
здесь единственное место в мини-аппе, где расшифровывается доступ к чужой
сети, и обращаться с ним одинаково во всех четырнадцати обработчиках можно
только видя их рядом.

ПРАВИЛО ЭТОГО МОДУЛЯ. Наружу уходит АДРЕС прокси, но не доступ к нему.
Выгрузка (`proxy_export`) и статистика (`proxy_stats`) маскировали пароль с
самого начала, а список (`proxies`) отдавал полный URL — потому что лежал от
них в двухстах строках, и расхождение никому не бросалось в глаза. Теперь
маскируют все три, а последний барьер в ответах API (`secret_masking`,
`URL_CRED_FIELD_NAMES`) гасит пароль в `proxy_url` даже у обработчика,
который напишут завтра.

Адрес, который уходит в Telethon, по пути НЕ расшифровывается: `proxy_url` в
`user_proxies` хранится зашифрованным, а разбирает его
`account_manager._parse_proxy` (с passthrough для старых строк в открытом
виде). Расшифровка в обработчике означала бы лишнее появление пароля в памяти
процесса.

Что маршруты не потерялись, стережёт слепок в
`tests/test_miniapp_routes_are_not_lost.py`.
"""
from __future__ import annotations

import asyncio

import asyncpg
from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Регистрирует маршруты прокси на общем приложении мини-аппа."""
    # Общие помощники живут в mini_app_api, а он импортирует этот модуль —
    # импорт сверху дал бы цикл. К моменту вызова mini_app_api уже загружен.
    from services.mini_app_api import (
        _INTERNAL_ERROR, _apply_proxy_moves, _check_all_proxies_core,
        _collect_valid_proxies, _csv_resp, _err, _get_uid, _json_resp,
        _proxy_display_host, _reject_proxy_reason, _safe_count, _safe_fetch,
        _safe_fetchrow, _safe_fetchval, log, parse_proxy_type,
    )
    from services.security import validate_integer, validate_string

    async def rotate_proxies(request: web.Request) -> web.Response:
        """Безопасная ротация назначений прокси по пулу (anti-detection), без потери
        изоляции. Меняет только proxy_id простаивающих аккаунтов внутри транзакции с
        FOR UPDATE; резолвер не трогаем. body: {account_ids?: [], proxy_ids?: []}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        acc_ids = body.get("account_ids") or []
        pool_ids_in = body.get("proxy_ids") or []
        from services import proxy_rotation
        try:
            # Единая реализация ротации (эффект+транзакция) — общая с ботом.
            result = await proxy_rotation.apply_rotation(pool, uid, acc_ids, pool_ids_in)
            return _json_resp(result)
        except Exception:
            log.exception("rotate_proxies uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def proxies(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Список был жёстко обрезан двумя сотнями без единого признака обрезки:
        # плитка «Всего» на экране пула считалась по этой же странице, а четыре
        # выпадашки назначения прокси показывали её как весь парк. У владельца
        # с 300 прокси триста первый нельзя было ни увидеть, ни назначить.
        try:
            limit = min(validate_integer(request.query.get("limit", "200"),
                                         min_val=1, max_val=1000) or 200, 1000)
        except (ValueError, TypeError):
            limit = 200
        try:
            offset = max(validate_integer(request.query.get("offset", "0"),
                                          min_val=0, max_val=100000) or 0, 0)
        except (ValueError, TypeError):
            offset = 0
        # is_backup может ещё не примениться (лаг миграции) — тогда селект с колонкой
        # упадёт; фолбэк без неё, чтобы список прокси НИКОГДА не ломался.
        # acc_count — сколько аккаунтов сидит на прокси. Без него мёртвый прокси
        # с двенадцатью аккаунтами и мёртвый запасной без единого выглядели в
        # списке одинаково, и было непонятно, какой чинить первым. Тем же числом
        # объясняется отказ удаления (409 «прокси назначен N аккаунтам») ДО тапа.
        try:
            rows = await pool.fetch(
                """SELECT id, label, proxy_url, proxy_type, is_active, is_alive, last_check,
                          created_at, COALESCE(is_backup, FALSE) AS is_backup,
                          -- latency_avg_ms пишет сторож прокси; экран «Пул»
                          -- читал его как latency_ms и потому ВСЕГДА показывал
                          -- «Нет данных о задержке» и среднее «—».
                          latency_avg_ms AS latency_ms,
                          (SELECT COUNT(*) FROM tg_accounts a
                            WHERE a.owner_id=$1 AND a.proxy_id=user_proxies.id) AS acc_count
                   FROM user_proxies WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT $2 OFFSET $3""", uid, limit, offset)
        except Exception:
            rows = await _safe_fetch(pool,
                """SELECT id, label, proxy_url, proxy_type, is_active, is_alive, last_check, created_at
                   FROM user_proxies WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT $2 OFFSET $3""", uid, limit, offset)
            rows = [dict(r, is_backup=False, acc_count=0, latency_ms=None) for r in rows]
        # proxy_url хранится зашифрованным. Расшифровываем для показа — но
        # МАСКИРУЕМ логин и пароль: экран печатает только хост (везде стоит
        # `(p.proxy_url||'').split('@').pop()`), а полный адрес с паролем
        # уходил клиенту просто потому, что так получилось. Выгрузка прокси
        # (proxy_export) маскирует его с тем же обоснованием — «не выгружаем
        # логин/пароль», — и docstring у _json_resp прямо обещает, что доступ
        # к прокси наружу не уйдёт. Обещание теперь выполняется и здесь.
        #
        # Единственное место, которому был нужен полный адрес, — проверка
        # сессии через выбранный прокси при импорте: экран брал URL из этого
        # ответа и отправлял его назад на сервер. Теперь сервер берёт адрес из
        # своего хранилища по proxy_id (см. services/session_importer.py).
        from services.proxy_hygiene import mask_proxy_url
        from services.token_vault import decrypt_token

        out = []
        for r in rows:
            d = dict(r)
            if d.get("proxy_url"):
                d["proxy_url"] = mask_proxy_url(decrypt_token(d["proxy_url"]))
            out.append(d)
        # Счётчики — по ВСЕМУ парку, а не по странице: иначе плитки на экране
        # пула утверждали бы то, чего не проверяли.
        total = await _safe_count(
            pool, "SELECT COUNT(*) FROM user_proxies WHERE owner_id=$1", uid)
        counts = {"total": total}
        try:
            agg = await pool.fetchrow(
                """SELECT COUNT(*) FILTER (WHERE is_alive IS TRUE)  AS alive,
                          COUNT(*) FILTER (WHERE is_alive IS FALSE) AS dead,
                          COUNT(*) FILTER (WHERE is_alive IS NULL)  AS unchecked,
                          COUNT(*) FILTER (WHERE COALESCE(is_backup, FALSE)) AS backup,
                          AVG(latency_avg_ms) FILTER (WHERE latency_avg_ms > 0) AS avg_latency
                     FROM user_proxies WHERE owner_id=$1""", uid)
            if agg:
                counts.update({
                    "alive": int(agg["alive"] or 0),
                    "dead": int(agg["dead"] or 0),
                    "unchecked": int(agg["unchecked"] or 0),
                    "backup": int(agg["backup"] or 0),
                    "avg_latency": int(agg["avg_latency"]) if agg["avg_latency"] else None,
                })
        except Exception as exc:
            # Колонок могло не быть (лаг миграции) — тогда отдаём только total,
            # а фронт честно подпишет плитки «Показано», а не «Всего».
            log.warning("proxies uid=%d: агрегат недоступен: %s", uid, exc)
        return _json_resp({
            "proxies": out,
            "total": total,
            "counts": counts,
            "page": {"offset": offset, "limit": limit,
                     "has_more": offset + len(out) < total},
        })

    async def add_proxy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        proxy_url = (body.get("proxy_url") or "").strip()
        label = (body.get("label") or "").strip() or None
        if not proxy_url:
            return _err("Нужен адрес прокси")
        # Единая валидация (паритет с import_proxies): длина, loopback, схема.
        _reason = _reject_proxy_reason(proxy_url)
        if _reason == "too_long":
            return _err("proxy_url слишком длинный (>500 символов)")
        if _reason == "internal":
            return _err("Внутренние/loopback-адреса недопустимы")
        if _reason == "scheme":
            return _err("Адрес прокси должен начинаться с socks5://, socks4:// или http://")
        proxy_type = parse_proxy_type(proxy_url)
        try:
            # шифруем at-rest; дедуп по детерминированному proxy_fp (шифр недетерминирован)
            from services.token_vault import encrypt_token, proxy_fingerprint

            _fp = proxy_fingerprint(proxy_url)
            _enc = encrypt_token(proxy_url)
            try:
                row = await pool.fetchrow(
                    """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type, proxy_fp)
                       VALUES($1,$2,$3,$4,$5)
                       ON CONFLICT(owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL DO UPDATE
                       SET label=EXCLUDED.label RETURNING id""",
                    uid, label, _enc, proxy_type, _fp)
            except asyncpg.UndefinedColumnError:
                # proxy_fp ещё не мигрирован (лаг деплоя schema_v146) — фолбэк без него,
                # чтобы «добавить прокси» работало ВСЕГДА (без прокси не работает ничего).
                # Шифротекст уникален → дублей на UNIQUE(owner_id, proxy_url) не будет.
                row = await pool.fetchrow(
                    """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type)
                       VALUES($1,$2,$3,$4) RETURNING id""",
                    uid, label, _enc, proxy_type)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as e:
            log.exception("add_proxy uid=%d", uid)
            return _err(f"Не удалось добавить прокси: {str(e)[:120]}", 400)

    async def delete_proxy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор прокси", 400)
        try:
            from services import proxy_hygiene
            # Гард изоляции живёт в одной двери на весь продукт: FK
            # tg_accounts.proxy_id = ON DELETE SET NULL, и удалить назначенный
            # прокси значит молча обнулить proxy_id аккаунтов — они уйдут
            # напрямую со своего IP и получат AUTH_KEY_DUPLICATED.
            res = await proxy_hygiene.delete_proxy_safely(pool, uid, proxy_id)
            if not res.get("ok"):
                code = 409 if res.get("reason") == "assigned" else 404
                return _err(proxy_hygiene.delete_refusal_text(res), code)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_proxy uid=%s proxy_id=%s", uid, proxy_id)
            return _err("Не удалось удалить прокси", 500)

    async def proxy_cleanup_dead(request: web.Request) -> web.Response:
        """Массово удалить подтверждённо-мёртвые (is_alive IS FALSE) НЕназначенные
        прокси. Назначенные и непроверенные не трогаем (изоляция). Read→delete."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Кандидаты: мёртвые пробой И не назначенные ни одному аккаунту.
            removed = await pool.fetch(
                """DELETE FROM user_proxies up
                   WHERE up.owner_id=$1 AND up.is_alive IS FALSE
                     AND NOT EXISTS (
                         SELECT 1 FROM tg_accounts a
                         WHERE a.owner_id=$1 AND a.proxy_id=up.id)
                   RETURNING id""", uid)
            # Пропущенные мёртвые (назначены) — для честного отчёта оператору.
            skipped = await _safe_fetchval(pool,
                """SELECT COUNT(*) FROM user_proxies up
                   WHERE up.owner_id=$1 AND up.is_alive IS FALSE
                     AND EXISTS (SELECT 1 FROM tg_accounts a
                                 WHERE a.owner_id=$1 AND a.proxy_id=up.id)""", uid) or 0
            # Прокси — секретоносный ресурс (proxy_url зашифрован), и одиночное
            # удаление журнал уже пишет. Массовое шло мимо него.
            if removed:
                from database.db import record_manual_action

                await record_manual_action(
                    pool, uid, "proxy_cleanup_dead",
                    target=",".join(str(r["id"]) for r in removed[:50]))
            return _json_resp({"ok": True, "removed": len(removed),
                               "skipped_assigned": int(skipped)})
        except Exception:
            log.exception("proxy_cleanup_dead uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def proxy_export(request: web.Request) -> web.Response:
        """Выгрузить список прокси (CSV/JSON) для аудита. Креды замаскированы —
        не выгружаем логин/пароль в файл. Показывает назначение и здоровье."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        fmt = request.query.get("format", "csv")
        try:
            from services import proxy_hygiene
            from services.token_vault import decrypt_token
            rows = await pool.fetch(
                """SELECT up.id, up.label, up.proxy_url, up.proxy_type, up.geo_country,
                          up.is_active, up.is_alive, up.last_check,
                          COALESCE(up.is_backup, FALSE) AS is_backup,
                          (SELECT COUNT(*) FROM tg_accounts a
                           WHERE a.owner_id=$1 AND a.proxy_id=up.id) AS assigned
                   FROM user_proxies up WHERE up.owner_id=$1 ORDER BY up.id""", uid)
            def _masked(enc):
                try:
                    return proxy_hygiene.mask_proxy_url(decrypt_token(enc))
                except Exception:
                    return proxy_hygiene.mask_proxy_url(enc)
            items = []
            for r in rows:
                items.append({
                    "id": r["id"], "label": r["label"] or "",
                    "url": _masked(r["proxy_url"]), "type": r["proxy_type"] or "",
                    "geo": r["geo_country"] or "", "active": bool(r["is_active"]),
                    "alive": (None if r["is_alive"] is None else bool(r["is_alive"])),
                    "last_check": str(r["last_check"] or ""), "backup": bool(r["is_backup"]),
                    "assigned": int(r["assigned"] or 0),
                })
            if fmt == "json":
                return _json_resp({"proxies": items})
            header = ["ID", "Метка", "URL (маска)", "Тип", "Гео", "Активен",
                      "Живой", "Проверен", "Резерв", "Назначен аккаунтам"]
            data = [[
                it["id"], it["label"], it["url"], it["type"], it["geo"],
                "да" if it["active"] else "нет",
                ("?" if it["alive"] is None else ("да" if it["alive"] else "нет")),
                it["last_check"], "да" if it["backup"] else "нет", it["assigned"],
            ] for it in items]
            return _csv_resp("proxies.csv", header, data)
        except Exception:
            log.exception("proxy_export uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def check_proxy(request: web.Request) -> web.Response:
        """Проверить живость прокси (probe → api.telegram.org), сохранить is_alive/last_check.
        Возможность test_proxy/check_proxy_health раньше была недоступна в UI."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор прокси", 400)
        row = await _safe_fetchrow(pool,
            "SELECT id, proxy_url FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, uid)
        if not row:
            return _err("Прокси не найден", 404)
        from services.proxy_selector import probe_proxy
        res = await probe_proxy(row["proxy_url"])
        try:
            await pool.execute(
                "UPDATE user_proxies SET is_alive=$1, last_check=now() WHERE id=$2 AND owner_id=$3",
                bool(res.get("ok")), proxy_id, uid)
        except Exception:
            # log_exc_swallow в этом модуле не импортирован — обработчик ошибки сам
            # падал бы NameError, превращая штатный сбой записи в 500 и теряя
            # результат уже выполненной проверки прокси.
            log.warning("check_proxy persist failed proxy=%s", proxy_id, exc_info=True)
        return _json_resp({"ok": True, "alive": bool(res.get("ok")),
                           "latency_ms": res.get("latency_ms"), "error": res.get("error")})

    async def proxy_toggle_backup(request: web.Request) -> web.Response:
        """Пометить/снять прокси как резервный (is_backup) для failover."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор прокси", 400)
        row = await _safe_fetchrow(pool,
            "SELECT COALESCE(is_backup, FALSE) AS is_backup FROM user_proxies "
            "WHERE id=$1 AND owner_id=$2", proxy_id, uid)
        if row is None:
            return _err("Прокси не найден", 404)
        new_val = not bool(row["is_backup"])
        try:
            await pool.execute(
                "UPDATE user_proxies SET is_backup=$1 WHERE id=$2 AND owner_id=$3",
                new_val, proxy_id, uid)
        except Exception as e:
            log.exception("proxy_toggle_backup uid=%s proxy=%s", uid, proxy_id)
            return _err(f"Не удалось изменить: {str(e)[:120]}", 400)
        return _json_resp({"ok": True, "is_backup": new_val})

    async def proxy_failover(request: web.Request) -> web.Response:
        """Failover: переназначить аккаунты с мёртвым прокси на живой резервный."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.proxy_selector import failover_dead_proxies
            res = await asyncio.wait_for(failover_dead_proxies(pool, uid), timeout=180)
            return _json_resp({"ok": True, **res})
        except asyncio.TimeoutError:
            return _err("Проверка прокси заняла слишком долго — повторите", 400)
        except Exception:
            log.exception("proxy_failover uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def proxy_evacuate(request: web.Request) -> web.Response:
        """Переселить аккаунты с мёртвых прокси на живые.

        Сторож прокси просит «замените прокси или переназначьте аккаунты на
        рабочий», но единственное автоматическое переназначение (failover)
        берёт только прокси, заранее помеченные резервными. У пользователя,
        который просто держит несколько живых прокси, оно не делало ничего, и
        аккаунты стояли намертво. Здесь — переезд на наименее загруженный
        живой прокси; аккаунт на мёртвом прокси не делает ВООБЩЕ ничего,
        поэтому переезд лучше простоя даже ценой неполной изоляции — но об
        ухудшении изоляции мы говорим прямо.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services.proxy_balancer import (
            isolation_note, isolation_summary, plan_evacuation,
        )
        from services.resource_selector import PROXY_DEAD_STREAK

        stranded = await _safe_fetch(pool,
            """SELECT a.id FROM tg_accounts a
                 JOIN user_proxies p ON p.id = a.proxy_id
                WHERE a.owner_id=$1 AND a.is_active
                  AND p.is_alive IS FALSE
                  AND COALESCE(p.consecutive_failures,0) >= $2
                ORDER BY a.id""", uid, PROXY_DEAD_STREAK)
        acc_ids = [int(r["id"]) for r in (stranded or [])]
        if not acc_ids:
            return _json_resp({"ok": True, "moved": 0,
                               "note": "Аккаунтов на мёртвых прокси нет"})
        live = await _safe_fetch(pool,
            """SELECT p.id,
                      (SELECT COUNT(*) FROM tg_accounts a
                        WHERE a.proxy_id = p.id) AS used
                 FROM user_proxies p
                WHERE p.owner_id=$1 AND p.is_active
                  AND COALESCE(p.is_alive, TRUE) IS TRUE""", uid)
        loads = [(int(r["id"]), int(r["used"] or 0)) for r in (live or [])]
        prior = dict(loads)
        plan = plan_evacuation(acc_ids, loads)
        try:
            moved = await _apply_proxy_moves(pool, uid, plan["moves"])
        except Exception:
            log.exception("proxy_evacuate: пакетный переезд не удался uid=%s", uid)
            return _err("Не удалось переселить аккаунты", 500)
        iso = isolation_summary([p for _a, p in plan["moves"]], prior)
        return _json_resp({
            "ok": True, "moved": moved,
            "stranded": len(plan["stranded"]),
            "isolation": iso,
            "isolation_note": isolation_note(iso),
            "note": (f"Переселено {moved} аккаунтов"
                     + (f"; {len(plan['stranded'])} остались без прокси — "
                        f"живых прокси не хватает" if plan["stranded"] else "")),
        })

    async def proxies_isolation_check(request: web.Request) -> web.Response:
        """Проверка уникальности IP: активные аккаунты, делящие один IP прокси
        (нарушение изоляции → риск бана), + аккаунты без прокси."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.proxy_selector import audit_proxy_isolation
            return _json_resp(await audit_proxy_isolation(pool, uid))
        except Exception:
            log.exception("proxies_isolation_check uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def check_all_proxies(request: web.Request) -> web.Response:
        """Проверить все прокси владельца (ограниченная конкурентность)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        return _json_resp(await _check_all_proxies_core(pool, uid))

    async def import_proxies(request: web.Request) -> web.Response:
        """Массовый импорт прокси: вставленный список (по одному на строку)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        from services.token_vault import encrypt_token, proxy_fingerprint
        raw = validate_string(body.get("proxies"), max_len=50000) or ""

        # Валидация/нормализация — в чистой _collect_valid_proxies (тестируется),
        # затем ОДИН bulk-INSERT вместо N round-trip'ов (до 500 строк → до 500
        # запросов). Паритет с add_proxy: шифруем at-rest (encrypt_token) и дедуп
        # по детерминированному proxy_fp — иначе bulk-импорт клал креды прокси в
        # БД ПЛЕЙНТЕКСТОМ и не отсекал дубли, добавленные одиночно.
        purls, skipped = _collect_valid_proxies(raw)
        encs = [encrypt_token(p) for p in purls]
        ptypes = [parse_proxy_type(p) for p in purls]
        fps = [proxy_fingerprint(p) for p in purls]

        added = 0
        if encs:
            try:
                rows = await pool.fetch(
                    """INSERT INTO user_proxies(owner_id, proxy_url, proxy_type, proxy_fp)
                       SELECT $1, u.enc, u.ptype, u.fp
                       FROM unnest($2::text[], $3::text[], $4::text[]) AS u(enc, ptype, fp)
                       ON CONFLICT(owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL DO NOTHING
                       RETURNING id""",
                    uid, encs, ptypes, fps)
                added = len(rows)
            except asyncpg.UndefinedColumnError:
                # proxy_fp ещё не мигрирован (лаг деплоя schema_v146) — фолбэк без
                # него, как в add_proxy. Шифротекст недетерминирован, поэтому
                # ON CONFLICT(proxy_url) дубли не отсечёт, но импорт не упадёт.
                rows = await pool.fetch(
                    """INSERT INTO user_proxies(owner_id, proxy_url, proxy_type)
                       SELECT $1, u.enc, u.ptype
                       FROM unnest($2::text[], $3::text[]) AS u(enc, ptype)
                       ON CONFLICT(owner_id, proxy_url) DO NOTHING
                       RETURNING id""",
                    uid, encs, ptypes)
                added = len(rows)

        duplicates = len(encs) - added   # прошли валидацию, но уже были в пуле
        return _json_resp({"ok": True, "added": added, "skipped": skipped,
                           "duplicates": duplicates})

    async def proxy_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import account_manager as _am, proxy_hygiene
            from services.token_vault import decrypt_token
            rows = await pool.fetch(
                """SELECT up.id, up.label, up.proxy_url, up.geo_country, up.is_active,
                          up.is_alive, up.last_check,
                          (SELECT COUNT(*) FROM tg_accounts a
                           WHERE a.owner_id=$1 AND a.proxy_id=up.id) AS assigned
                   FROM user_proxies up WHERE up.owner_id=$1 ORDER BY up.id""",
                uid,
            )
            stats = []
            for r in rows:
                # runtime-статы keyed по СТРОКЕ proxy_url из БД (тот же шифротекст
                # используется в test_proxy → ключи совпадают); в UI показываем
                # МАСКИРОВАННЫЙ расшифрованный url, а не сырой ENC:-шифротекст.
                s = _am.get_proxy_stats(r["proxy_url"])
                try:
                    disp = proxy_hygiene.mask_proxy_url(decrypt_token(r["proxy_url"]))
                except Exception:
                    disp = proxy_hygiene.mask_proxy_url(r["proxy_url"])
                stats.append({
                    "id": r["id"],
                    "label": r["label"] or "",
                    "url": disp,
                    "geo": r["geo_country"],
                    "active": bool(r["is_active"]),
                    "alive": (None if r["is_alive"] is None else bool(r["is_alive"])),
                    "last_check": str(r["last_check"] or ""),
                    "assigned": int(r["assigned"] or 0),
                    **s,
                })
            return _json_resp({"proxies": stats})
        except Exception:
            log.exception("proxy_stats uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/proxies", proxies)
    app.router.add_post("/api/miniapp/proxy", add_proxy)
    app.router.add_post("/api/miniapp/proxy/cleanup_dead", proxy_cleanup_dead)
    app.router.add_post("/api/miniapp/proxy/rotate", rotate_proxies)
    app.router.add_get("/api/miniapp/proxy/export", proxy_export)
    app.router.add_delete("/api/miniapp/proxy/{proxy_id}", delete_proxy)
    app.router.add_post("/api/miniapp/proxy/{proxy_id}/check", check_proxy)
    app.router.add_post("/api/miniapp/proxy/{proxy_id}/backup", proxy_toggle_backup)
    app.router.add_post("/api/miniapp/proxy/failover", proxy_failover)
    app.router.add_post("/api/miniapp/proxy/evacuate", proxy_evacuate)
    app.router.add_post("/api/miniapp/proxies/check_all", check_all_proxies)
    app.router.add_get("/api/miniapp/proxies/isolation_check", proxies_isolation_check)
    app.router.add_post("/api/miniapp/proxies/import", import_proxies)
    app.router.add_get("/api/miniapp/proxy_stats", proxy_stats)
