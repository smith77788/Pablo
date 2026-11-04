"""Маршруты загрузки медиа с устройства (см. services/media_uploads.py)."""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
        read_upload,
    )
    from services.mini_app_integrations import _public_base
    from services import media_uploads as mu

    async def media_upload(request: web.Request) -> web.Response:
        """POST /api/miniapp/media/upload (multipart, поле file) → {url}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        if not (request.headers.get("Content-Type") or "").lower().startswith("multipart/"):
            return _err("Файл не приложен", 400)
        try:
            blob = b""
            reader = await request.multipart()
            async for part in reader:
                if part.name == "file":
                    try:
                        blob = await read_upload(part, mu.MAX_BYTES)
                    except ValueError:
                        return _err(f"Файл больше {mu.MAX_BYTES // (1024 * 1024)} МБ", 413)
                    break
            res = await mu.store(pool, uid, blob)
        except mu.UploadRejected as e:
            return _err(str(e), 400)
        except Exception as exc:
            bad = bad_body_response(request, exc)
            if bad is not None:
                return bad
            log.exception("media_upload uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "url": mu.public_url(_public_base(request), res["token"]),
                           "mime": res["mime"], "size": res["size"]})

    async def media_get(request: web.Request) -> web.Response:
        """GET /media/u/{token}: без входа, ссылку знает только владелец."""
        got = await mu.load_public(pool, request.match_info.get("token", ""))
        if not got:
            return web.Response(status=404, text="not found")
        mime, data = got
        return web.Response(body=data, headers={
            "Content-Type": mime,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=86400",
            "Referrer-Policy": "no-referrer",
        })

    app.router.add_post("/api/miniapp/media/upload", media_upload)
    app.router.add_get("/media/u/{token}", media_get)
