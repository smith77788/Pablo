"""Пул релеев Cloudflare — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 6 маршрутов `/api/miniapp/cf/*` —
состояние пула, развёртывание, ход развёртывания, проверка живости воркеров,
раздача адресов аккаунтам без релея и сверка пула.

Доступы Cloudflare достаёт `mini_app_api.cf_resolve_creds(pool, uid)`: он
поднят на уровень модуля, потому что его зовёт ещё и сводка о прокси в
монолите, а замыкание внутри `setup_routes` импортировать нельзя. Доступы в ответ не уходят и в лог
не пишутся — в ответе только признак «заданы или нет».

Развёртывание идёт В ФОНЕ и отвечает сразу: создание воркеров занимает минуты,
а шлюз рвёт HTTP-запрос по таймауту, и владелец видел «сервис недоступен» на
работающей операции. Ход — отдельным маршрутом `/cf/pool/status`.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты пула релеев Cloudflare."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, cf_resolve_creds, log,
    )
    from services.security import validate_string


    async def cf_pool_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.cf_pool_manager import get_pool_status
            status = await get_pool_status(pool, uid)
            c = await cf_resolve_creds(pool, uid)
            # доступы — без утечки токена: только флаги/некритичные поля
            status["credentials"] = {
                "has_token": c["has_token"],
                "account_id": c["account_id"],
                "subdomain": c["subdomain"],
                "ready": bool(c["has_token"] and c["account_id"]),
            }
            # последний результат фонового деплоя (running/ok/errors) — чтобы
            # пользователь видел РЕАЛЬНУЮ причину, а не пустой результат.
            # process-local: не переживает рестарт, не шарится между воркерами.
            last = (request.app.get("_cf_deploy_last") or {}).get(uid)
            if last:
                status["last_deploy"] = last
            return _json_resp(status)
        except Exception:
            log.exception("cf_pool_status uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def cf_credentials_save(request: web.Request) -> web.Response:
        """Сохранить доступы CF в приложении (без Railway). Токен шифруется в БД.
        body: {api_token?, account_id, subdomain}. Пустой api_token не затирает."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        account_id = validate_string(data.get("account_id"), max_len=64) or ""
        subdomain = validate_string(data.get("subdomain"), max_len=80) or ""
        api_token = (data.get("api_token") or "").strip()
        if not account_id:
            return _err("Укажите Account ID", 400)
        try:
            from database import db
            await db.set_cf_credentials(pool, uid, api_token or None, account_id, subdomain)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("cf_credentials_save uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def cf_pool_deploy(request: web.Request) -> web.Response:
        """Деплой пула CF Workers. Долгая операция (N×2 запросов к CF API) → НЕ
        блокируем HTTP-запрос (иначе шлюз рвёт по таймауту → «ответ не JSON/сервис
        недоступен»). Запускаем в ФОНЕ, отвечаем сразу; прогресс — через /status."""
        import asyncio as _asyncio
        import time as _time
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            data = {}
        raw_count = data.get('count')
        # count не задан / 'auto' / 0 → по числу аккаунтов, которым реально нужен
        # релей (активные без своего прокси) → изоляция 1:1 без ручного счёта.
        if raw_count in (None, '', 'auto', 0, '0'):
            try:
                from services.cf_pool_manager import count_relay_targets
                ct = await count_relay_targets(pool, uid)
                count = ct["relay_needed"] or ct["active"] or 1
            except Exception:
                log.exception("cf auto-count uid=%s", uid)
                count = 10
        else:
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                count = 10
        count = max(1, min(count, 100))
        prefix = data.get('name_prefix') or f'tg-relay-{uid}'
        c = await cf_resolve_creds(pool, uid)
        if not c["api_token"] or not c["account_id"]:
            return _err("Не заданы доступы Cloudflare (токен + Account ID). "
                        "Впишите их в приложении или в переменных Railway.", 400)

        # process-local карта последнего результата деплоя по uid (для /status).
        # не переживает рестарт, не шарится между воркерами — только для UX-фидбэка.
        _last = request.app.setdefault("_cf_deploy_last", {})
        _last[uid] = {"running": True, "ok": 0, "count": count, "errors": [], "ts": int(_time.time())}

        async def _bg_deploy():
            try:
                from services.cf_pool_manager import (deploy_pool,
                    assign_urls_to_accounts, reconcile_pool)
                res = await deploy_pool(count, prefix, c["api_token"], c["account_id"],
                                        subdomain=c["subdomain"])
                urls = res.get("urls", [])
                if urls:
                    await assign_urls_to_accounts(pool, uid, urls)
                    # снести воркеры, которых больше нет в наборе (старый больший пул)
                    try:
                        await reconcile_pool(pool, uid, urls, c["api_token"],
                                             c["account_id"])
                    except Exception:
                        log.exception("cf reconcile uid=%s", uid)
                _last[uid] = {"running": False, "ok": len(urls), "count": count,
                              "errors": res.get("errors", []), "ts": int(_time.time())}
                log.info("cf deploy done uid=%s: %d/%d воркеров", uid, len(urls), count)
            except Exception as e:
                log.exception("cf background deploy uid=%s", uid)
                _last[uid] = {"running": False, "ok": 0, "count": count,
                              "errors": [str(e)[:300]], "ts": int(_time.time())}

        task = _asyncio.create_task(_bg_deploy())
        _tasks = request.app.setdefault("_cf_deploy_tasks", set())
        _tasks.add(task)
        task.add_done_callback(lambda t: _tasks.discard(t))
        return _json_resp({"ok": True, "started": True, "count": count})

    async def cf_pool_check(request: web.Request) -> web.Response:
        """Пинг всех воркеров (/health) — живость + гео (colo) + реальные egress-IP.
        Аналог «Проверить все» для прокси, но для CF-релея."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.cf_pool_manager import check_pool
            return _json_resp(await check_pool(pool, uid))
        except Exception:
            log.exception("cf_pool_check uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def cf_pool_assign(request: web.Request) -> web.Response:
        """Раздать существующий пул аккаунтам без релея (напр. добавленным после
        деплоя) — без пересоздания воркеров. Прокси-аккаунты не трогаются."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.cf_pool_manager import sync_relay_assignment
            return _json_resp(await sync_relay_assignment(pool, uid))
        except Exception:
            log.exception("cf_pool_assign uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def cf_pool_clear(request: web.Request) -> web.Response:
        """Снести пул: удалить воркеры в CF, очистить БД, снять cf_relay_url."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.cf_pool_manager import clear_pool
            c = await cf_resolve_creds(pool, uid)
            return _json_resp(await clear_pool(pool, uid, c["api_token"],
                                               c["account_id"]))
        except Exception:
            log.exception("cf_pool_clear uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/cf/pool/status", cf_pool_status)
    app.router.add_post("/api/miniapp/cf/pool/deploy", cf_pool_deploy)
    app.router.add_post("/api/miniapp/cf/pool/check", cf_pool_check)
    app.router.add_post("/api/miniapp/cf/pool/assign", cf_pool_assign)
    app.router.add_post("/api/miniapp/cf/pool/clear", cf_pool_clear)
    app.router.add_post("/api/miniapp/cf/credentials", cf_credentials_save)
