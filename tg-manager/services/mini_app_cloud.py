"""Telegram-облако — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 7 маршрутов `/api/miniapp/cloud/*` —
сводка по хранилищу, список файлов, загрузка, скачивание, удаление, папки и
квота.

Файлы лежат в Telegram зашифрованными, ключ у владельца; этот файл только
разбирает запрос, скоупит по владельцу и отдаёт русские ошибки. Загрузка
читается с ПРЕДЕЛОМ в процессе чтения (`read_upload`), а не целиком в память:
объявленный, но не действующий предел однажды позволял положить шлюз мини-аппа
одним запросом.

Локальный `_read_upload` уехал вместе с ручками — других потребителей у него
нет. `_cloud_safe_name` и `_content_disposition` остались на месте: они
модульные и чистые, их импортируем.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты Telegram-облака на приложении мини-аппа."""
    from services.mini_app_api import (
        _BAD_BODY, _INTERNAL_ERROR, _cloud_safe_name, _content_disposition, _err,
        _get_uid, _is_admin, _json_resp, log, read_upload, read_upload_text,
        bad_body_response,
    )

    async def _read_upload(part, max_bytes: int) -> bytes:
        """Прочитать multipart-part с жёстким лимитом (защита от OOM).

        Один источник правды — модульный `read_upload`: тот же помощник нужен
        модулям `services/mini_app_*.py`, а вложенная копия им недоступна.
        `UploadTooLarge` — наследник `ValueError`, поэтому прежние обработчики
        `except ValueError` продолжают ловить его как раньше.
        """
        return await read_upload(part, max_bytes)

    async def cloud_status(request: web.Request) -> web.Response:
        """Доступ + квота: сколько занято/лимит, платный ли доступ, безлимит ли."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services import tg_cloud
            q = await tg_cloud.get_quota(pool, uid)
            limit = await tg_cloud._effective_limit(pool, uid)
            return _json_resp({
                'access': await tg_cloud.has_access(pool, uid),
                'admin': tg_cloud._is_admin(uid),
                'used_bytes': q['used_bytes'],
                'limit_bytes': limit,          # 0 = безлимит
                'paid': q['paid'],
                # Потолок ОДНОЙ загрузки: мини-апп обязан отказать до чтения
                # файла, иначе пользователь ждёт загрузку ради отказа в конце.
                'max_upload_bytes': tg_cloud.MAX_FILE_BYTES,
            })
        except Exception:
            log.exception("cloud_status")
            return _err(_INTERNAL_ERROR, 500)

    async def cloud_list(request: web.Request) -> web.Response:
        """Список файлов владельца (манифест + здоровье избыточности, без содержимого)."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services import tg_cloud
            files = await tg_cloud.list_files(pool, uid)
            for f in files:
                try:
                    dur = await tg_cloud.file_durability(pool, int(f['id']))
                    f['min_replicas'] = dur['min_live']   # 0 = есть недоступный кусок
                    f['target_replicas'] = dur['target']
                except Exception:
                    f['min_replicas'] = None
            return _json_resp({'files': files, 'target_replicas': tg_cloud.REPLICAS})
        except Exception:
            log.exception("cloud_list")
            return _err(_INTERNAL_ERROR, 500)

    async def cloud_heal(request: web.Request) -> web.Response:
        """Восстановить избыточность файла (долить реплики на уцелевших хранителях)
        после бана части флота. Доступно владельцу файла."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info['file_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор файла", 400)
        try:
            from services import tg_cloud
            if not await tg_cloud.get_file(pool, uid, fid):
                return _err("файл не найден", 404)
            res = await tg_cloud.heal_file(pool, uid, fid)
            return _json_resp(res)
        except Exception:
            log.exception("cloud_heal")
            return _err(_INTERNAL_ERROR, 500)

    async def cloud_upload(request: web.Request) -> web.Response:
        """Загрузить файл в облако.

        Основной путь — multipart (поле `file`): тело читается потоком, поэтому
        размер ограничивает ТОЛЬКО наш потолок. Путь JSON {name, data_b64}
        оставлен для совместимости, но у него жёсткий предел сверху: aiohttp
        буферизует такое тело целиком и рубит его на `client_max_size` (1 МБ по
        умолчанию), а base64 раздувает файл на треть — то есть через JSON не
        проходил файл крупнее ~750 КБ. Отказ при этом прилетал из aiohttp
        по-английски и превращался в «внутреннюю ошибку» на экране.
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        from services import tg_cloud
        cap_mb = tg_cloud.MAX_FILE_BYTES // (1024 * 1024)
        too_big = f"файл больше предела одной загрузки ({cap_mb} МБ)"
        try:
            import base64 as _b64
            ctype = (request.headers.get('Content-Type') or '').lower()
            if ctype.startswith('multipart/'):
                name, blob, got_part = '', b'', False
                reader = await request.multipart()
                async for part in reader:
                    if part.name == 'name':
                        name = (await read_upload_text(part) or '').strip()
                    elif part.name in ('file', 'data'):
                        got_part = True
                        if not name:
                            name = (part.filename or '').strip()
                        try:
                            blob = await _read_upload(part, tg_cloud.MAX_FILE_BYTES)
                        except ValueError:
                            return _err(too_big, 413)
                if not got_part:
                    return _err("файл не приложен (ожидалось поле file)", 400)
            else:
                try:
                    d = await request.json()
                except web.HTTPRequestEntityTooLarge:
                    return _err(too_big, 413)
                except Exception:
                    return _err("не удалось разобрать запрос", 400)
                name = (d.get('name') or '').strip()
                raw = d.get('data_b64') or ''
                try:
                    # validate=True: без него base64 молча выбрасывает всё, что не
                    # из алфавита, и битая передача превращается в укороченный
                    # файл, который проходит проверку целостности как «целый».
                    blob = _b64.b64decode(raw, validate=True)
                except Exception:
                    return _err("не удалось прочитать данные файла", 400)
            if not blob:
                return _err("файл пустой", 400)
            if len(blob) > tg_cloud.MAX_FILE_BYTES:
                return _err(too_big, 413)
            f = await tg_cloud.store_file(pool, uid, _cloud_safe_name(name), blob)
            return _json_resp({'file': f})
        except tg_cloud.AccessDenied as e:
            return _err(str(e) or "нет доступа к облаку — требуется подписка", 403)
        except tg_cloud.QuotaExceeded as e:
            return _err(str(e), 413)
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("cloud_upload")
            return _err(_INTERNAL_ERROR, 500)

    async def cloud_download(request: web.Request) -> web.Response:
        """Скачать файл: собираем куски, расшифровываем, отдаём как поток байт."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info['file_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор файла", 400)
        try:
            from services import tg_cloud
            meta = await tg_cloud.get_file(pool, uid, fid)
            if not meta:
                return _err("файл не найден", 404)
            data = await tg_cloud.retrieve_file(pool, uid, fid)
            fname = _cloud_safe_name(meta.get('name'))
            # Реальный MIME по расширению: помогает нативному загрузчику Telegram
            # назвать файл и корректно проиграть видео/аудио в предпросмотре.
            import mimetypes
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            return web.Response(
                body=data,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Content-Type": ctype,
                    # Тип берётся из имени файла, то есть из данных пользователя —
                    # nosniff не даёт браузеру передумать и исполнить содержимое.
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": _content_disposition(fname),
                })
        except FileNotFoundError:
            return _err("файл не найден", 404)
        except Exception:
            log.exception("cloud_download")
            return _err(_INTERNAL_ERROR, 500)

    async def cloud_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info['file_id'])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор файла", 400)
        try:
            from services import tg_cloud
            ok = await tg_cloud.delete_file(pool, uid, fid)
            return _json_resp({'deleted': bool(ok)})
        except Exception:
            log.exception("cloud_delete")
            return _err(_INTERNAL_ERROR, 500)

    async def cloud_set_paid(request: web.Request) -> web.Response:
        """Выдать/снять платный доступ к облаку другому пользователю. Только
        владелец платформы (админ)."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services import tg_cloud
            if not tg_cloud._is_admin(uid):
                return _err("только владелец платформы может выдавать доступ", 403)
            d = await request.json()
            target = int(d.get('owner_id') or 0)
            if not target:
                return _err("нужен owner_id", 400)
            lim = d.get('limit_bytes')
            q = await tg_cloud.set_paid(pool, target, bool(d.get('paid', True)),
                                        limit_bytes=(int(lim) if lim else None))
            return _json_resp({'quota': q})
        except Exception:
            log.exception("cloud_set_paid")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/cloud/status", cloud_status)
    app.router.add_get("/api/miniapp/cloud/files", cloud_list)
    app.router.add_post("/api/miniapp/cloud/upload", cloud_upload)
    app.router.add_get("/api/miniapp/cloud/file/{file_id}/download", cloud_download)
    app.router.add_post("/api/miniapp/cloud/file/{file_id}/delete", cloud_delete)
    app.router.add_post("/api/miniapp/cloud/file/{file_id}/heal", cloud_heal)
    app.router.add_post("/api/miniapp/cloud/set-paid", cloud_set_paid)
