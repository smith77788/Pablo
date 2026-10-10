"""«Хранилище» (Echo Vault): архив деловой переписки, строго по владельцу.

Бизнес-соединение бота складывает переписку в `vault_messages`; эти
обработчики её показывают, ищут по ней, отдают медиа, экспортируют и позволяют
ответить из мини-аппа. Вся работа с данными — в `services/vault_service.py`,
аналитика — в `services/vault_analytics.py`; на соседей по `mini_app_api`
группа опирается только общими помощниками.

Вынесено из `services/mini_app_api.py`: тот файл — больше 23 тысяч строк и 926
маршрутов, зарегистрированных в одной функции.

Что маршруты не потерялись, стережёт слепок в
`tests/test_miniapp_routes_are_not_lost.py`.
"""
from __future__ import annotations

import json

from aiohttp import web


# Что показываем на месте, а не отдаём файлом. Список намеренно короткий и
# закрытый: тип приходит от собеседника, и всё незнакомое безопаснее отдать
# вложением. `image/svg+xml` здесь НЕТ — SVG исполняет скрипты.
_INLINE_SAFE_MIME = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    "image/heic", "image/heif", "image/avif",
    "video/mp4", "video/webm", "video/quicktime", "video/3gpp",
    "audio/mpeg", "audio/ogg", "audio/mp4", "audio/aac", "audio/wav",
    "audio/webm", "audio/flac", "audio/x-wav",
    "application/pdf",
})


def setup_routes(app: web.Application, pool) -> None:
    """Регистрирует маршруты «Хранилища» на общем приложении мини-аппа."""
    # Общие помощники живут в mini_app_api, а он импортирует этот модуль —
    # импорт сверху дал бы цикл. К моменту вызова mini_app_api уже загружен.
    from services.mini_app_api import (
        _INTERNAL_ERROR, _bot_token, _err, _get_uid, _json_resp,
        _resolve_bot_username, _safe_fetch, _vault_export_html, log,
    )

    async def vault_status(request: web.Request) -> web.Response:
        """Подключено ли бизнес-соединение + инструкция, если нет."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import vault_service as _v
        conn = await _v.active_connection_for_owner(pool, uid)
        botname = await _resolve_bot_username()
        prefs = await _v.get_notify_prefs(pool, uid)
        # Диагностика «зависания»: почему нет свежих диалогов (отключено / тихо
        # отвалилось / нет Premium). Fail-open — статус не должен падать из-за неё.
        try:
            diag = await _v.diagnostics(pool, uid)
        except Exception:
            log.exception("vault diagnostics uid=%s", uid)
            diag = {}
        return _json_resp({
            "connected": bool(conn),
            "can_reply": bool(conn.get("can_reply")) if conn else False,
            "bot_username": botname,
            "notify_deleted": prefs["notify_deleted"],
            "notify_edited": prefs["notify_edited"],
            "diagnostics": diag,
        })

    async def vault_analytics(request: web.Request) -> web.Response:
        """Аналитика диалогов Хранилища за период: объёмы, скорость ответа, часы.
        Свёртка — чистая vault_analytics.analyze_dialogs. fail-open."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.query.get("days", "30"))
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 180))
        try:
            rows = await _safe_fetch(pool,
                "SELECT chat_id, direction, msg_date, peer_name, peer_username FROM vault_messages "
                "WHERE owner_id=$1 AND msg_date > NOW() - ($2 || ' days')::interval",
                uid, str(days))
            from services import vault_analytics
            data = vault_analytics.analyze_dialogs([dict(r) for r in (rows or [])])
            data["days"] = days
            return _json_resp(data)
        except Exception:
            log.exception("vault_analytics uid=%s", uid)
            return _json_resp({"total": 0, "days": days})

    async def vault_chats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import vault_service as _v
        try:
            chats = await _v.list_chats(pool, uid, limit=200)
        except Exception:
            log.exception("vault_chats uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"chats": chats, "total": len(chats)})

    async def vault_messages(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            chat_id = int(request.match_info["chat_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор чата", 400)
        try:
            # max(1, ...) обязателен: без него ?limit=-5 уезжал в SQL как
            # LIMIT -5, Postgres отвечал ошибкой, и экран отдавал 500 вместо
            # данных — то есть параметр из запроса ронял эндпоинт.
            limit = max(1, min(int(request.query.get("limit", 200)), 1000))
            offset = max(int(request.query.get("offset", 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 200, 0
        q = request.query
        filters = {
            "media_only": q.get("media_only") in ("1", "true"),
            "deleted_only": q.get("deleted_only") in ("1", "true"),
            "edited_only": q.get("edited_only") in ("1", "true"),
            "direction": q.get("direction") if q.get("direction") in ("in", "out") else None,
        }
        from services import vault_service as _v
        try:
            # По умолчанию отдаём КОНЕЦ переписки: раньше всегда шли первые 200
            # с начала архива, и в длинном чате свежие сообщения были
            # недостижимы вовсе. offset здесь — шаг назад по истории.
            res = await _v.list_messages(pool, uid, chat_id, limit=limit, offset=offset,
                                         filters=filters, newest_first=True)
        except Exception:
            log.exception("vault_messages uid=%s chat=%s", uid, chat_id)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"messages": res["messages"], "has_more": res["has_more"],
                           "limit": limit, "offset": offset})

    async def vault_recent(request: web.Request) -> web.Response:
        """Лента «Недавно удалённое/изменённое» по всем чатам (экран «ловца»)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        kind = request.query.get("type", "deleted")
        if kind not in ("deleted", "edited"):
            kind = "deleted"
        from services import vault_service as _v
        try:
            items = await _v.recent_activity(pool, uid, kind, limit=100)
        except Exception:
            log.exception("vault_recent uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"items": items, "kind": kind, "total": len(items)})

    async def vault_settings(request: web.Request) -> web.Response:
        """GET — текущие настройки уведомлений; POST — переключить."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import vault_service as _v
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return _err("Неверный запрос", 400)
            await _v.set_notify_prefs(
                pool, uid,
                notify_deleted=body.get("notify_deleted"),
                notify_edited=body.get("notify_edited"))
        prefs = await _v.get_notify_prefs(pool, uid)
        return _json_resp({"notify_deleted": prefs["notify_deleted"],
                           "notify_edited": prefs["notify_edited"]})

    async def vault_export(request: web.Request) -> web.Response:
        """Экспорт архива (весь или ?chat_id=) в файл: format=json|html."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        chat_id = None
        if request.query.get("chat_id"):
            try:
                chat_id = int(request.query["chat_id"])
            except (TypeError, ValueError):
                return _err("Неверный идентификатор чата", 400)
        fmt = request.query.get("format", "html")
        from services import vault_service as _v
        try:
            data = await _v.export_data(pool, uid, chat_id)
        except Exception:
            log.exception("vault_export uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if fmt == "json":
            body = json.dumps(data, ensure_ascii=False, indent=2)
            ctype, ext = "application/json; charset=utf-8", "json"
        else:
            body = _vault_export_html(data)
            ctype, ext = "text/html; charset=utf-8", "html"
        return web.Response(
            body=body.encode("utf-8"), content_type=ctype.split(";")[0],
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="vault_export.{ext}"',
                "X-Export-Truncated": "1" if data.get("truncated") else "0",
                "X-Export-Count": str(data.get("exported", 0)),
            })

    async def vault_search(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = (request.query.get("q") or "").strip()
        if len(q) < 2:
            return _err("Запрос слишком короткий (минимум 2 символа)", 400)
        from services import vault_service as _v
        try:
            res = await _v.search_messages(pool, uid, q, limit=100)
        except Exception:
            log.exception("vault_search uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        # complete=False → архив просмотрен не до конца. Без этого «ничего не
        # найдено» было бы неправдой: раньше поиск молча брал только последние
        # 4000 сообщений.
        return _json_resp({
            "results": res["results"],
            "total": len(res["results"]),
            "scanned": res["scanned"],
            "archive_total": res["total"],
            "complete": res["complete"],
        })

    async def vault_media(request: web.Request) -> web.Response:
        """Отдать вложение из архива.

        media_file_id писался в базу с самого начала и не отдавался наружу
        ничем: в архиве была видна подпись «📷 Фото», а самого файла не
        существовало ни в одном экране. Для сообщений, которые собеседник
        УДАЛИЛ, это ломало главное обещание хранилища.

        Файл проксируем через сервер: прямая ссылка Telegram содержит токен
        бота, отдавать её в мини-апп нельзя.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            chat_id = int(request.match_info["chat_id"])
            msg_id = int(request.match_info["msg_id"])
        except (KeyError, ValueError):
            return _err("Неверный запрос", 400)
        token = _bot_token()
        if not token:
            return _err("Бот не настроен", 500)
        from aiogram import Bot as _Bot
        from services import vault_service as _v

        _b = _Bot(token=token)
        try:
            res = await _v.fetch_media(pool, _b, uid, chat_id, msg_id)
        except Exception:
            log.exception("vault_media uid=%s chat=%s msg=%s", uid, chat_id, msg_id)
            return _err(_INTERNAL_ERROR, 500)
        finally:
            try:
                await _b.session.close()
            except Exception:
                pass
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось получить файл",
                        int(res.get("status") or 404))
        name = str(res.get("filename") or "file")
        # Имя файла задаёт собеседник — в заголовок оно уходить как есть не
        # должно (перевод строки/кавычка ломают ответ). Кладём безопасный ASCII
        # плюс RFC 5987 для юникодного имени.
        import urllib.parse as _up
        ascii_name = "".join(c for c in name if 32 <= ord(c) < 127 and c not in '"\\') or "file"
        # Тип файла тоже задаёт собеседник. Показывать на месте (`inline`) можно
        # только то, что браузер не исполняет: иначе присланный `.html` или
        # `.svg` выполнится на НАШЕМ origin, а там в localStorage лежит
        # `ig_device_token` — ключ автономного входа в аккаунт.
        mime = (str(res.get("mime") or "").split(";")[0]).strip().lower()
        inline = mime in _INLINE_SAFE_MIME
        return web.Response(
            body=res["data"],
            headers={
                "Content-Type": mime if inline else "application/octet-stream",
                "Content-Disposition":
                    f'{"inline" if inline else "attachment"}; '
                    f'filename="{ascii_name}"; '
                    f"filename*=UTF-8''{_up.quote(name)}",
                # Браузер не должен передумывать насчёт типа сам: иначе
                # «текстовый» файл с разметкой внутри он покажет как страницу.
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=300",
            },
        )

    async def vault_reply(request: web.Request) -> web.Response:
        """Ответ собеседнику ОТ ИМЕНИ пользователя через бизнес-соединение."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            chat_id = int(request.match_info["chat_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        text = (body.get("text") or "").strip()
        if not text:
            return _err("Введите текст сообщения", 400)
        from services import vault_service as _v
        token = _bot_token()
        if not token:
            return _err("Бот не настроен", 500)
        from aiogram import Bot as _Bot
        _b = _Bot(token=token)
        try:
            res = await _v.send_reply(pool, _b, uid, chat_id, text)
        except Exception:
            log.exception("vault_reply uid=%s chat=%s", uid, chat_id)
            return _err(_INTERNAL_ERROR, 500)
        finally:
            try:
                await _b.session.close()
            except Exception:
                pass
        if not res.get("ok"):
            return _err(res.get("error") or "Не отправлено", 400)
        return _json_resp({"ok": True, "message_id": res.get("message_id")})

    # «Хранилище» (Echo Vault)
    app.router.add_get("/api/miniapp/vault/status", vault_status)
    app.router.add_get("/api/miniapp/vault/analytics", vault_analytics)
    app.router.add_get("/api/miniapp/vault/chats", vault_chats)
    app.router.add_get("/api/miniapp/vault/chat/{chat_id}/messages", vault_messages)
    app.router.add_get("/api/miniapp/vault/search", vault_search)
    app.router.add_get("/api/miniapp/vault/recent", vault_recent)
    app.router.add_get("/api/miniapp/vault/export", vault_export)
    app.router.add_get("/api/miniapp/vault/settings", vault_settings)
    app.router.add_post("/api/miniapp/vault/settings", vault_settings)
    app.router.add_post("/api/miniapp/vault/chat/{chat_id}/reply", vault_reply)
    app.router.add_get("/api/miniapp/vault/media/{chat_id}/{msg_id}", vault_media)
