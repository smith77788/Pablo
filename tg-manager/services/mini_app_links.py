"""Сокращатель ссылок — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: пять маршрутов
`/api/miniapp/links*` — создать, список, выключить, удалить, QR.

Публичный редирект по короткому коду (`/{code}` и `/s/{code}`) остался в
монолите нарочно: он не часть API мини-аппа, а корневой вход, и регистрируется
вместе с остальными корневыми путями.

`_short_base` и `_self_hosts` подняты в `mini_app_api` на модульный уровень и
импортируются отсюда: они чистые (зависят только от request), и их же зовёт тот
самый редирект. `_short_base` важен для ответа: короткая ссылка без базового
адреса — это код без адреса, по которому её не открыть.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты сокращателя ссылок на приложении мини-аппа."""
    from services import link_shortener as _lnk
    from services.mini_app_api import (
        _err, _get_uid, _json_resp, _list_limit, _list_offset, _self_hosts,
        _short_base,
    )
    from services.security import validate_string

    async def links_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        target = validate_string(body.get("target_url") or body.get("url"), max_len=2048)
        title = validate_string(body.get("title"), max_len=200, required=False)
        code = validate_string(body.get("code"), max_len=40, required=False)
        tags = validate_string(body.get("tags"), max_len=400, required=False)
        expires_at = validate_string(body.get("expires_at"), max_len=40, required=False)
        _utm_in = body.get("utm") if isinstance(body.get("utm"), dict) else {}
        utm = {k: validate_string(_utm_in.get(k), max_len=100, required=False) or ""
               for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")}
        res = await _lnk.create(pool, uid, target or "", title,
                                self_hosts=_self_hosts(request),
                                custom_code=(code or None), utm=utm,
                                tags=(tags or None), expires_at=(expires_at or None))
        if not res.get("ok"):
            return _err(res.get("error") or "Не удалось создать ссылку")
        res["short_url"] = f"{_short_base(request)}/{res['code']}"
        if body.get("qr"):
            res["qr"] = _lnk.make_qr_data_uri(res["short_url"])
        return _json_resp(res)

    async def links_qr(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        code = validate_string(body.get("code"), max_len=40)
        link = await _lnk.get_one(pool, uid, code or "")
        if not link:
            return _err("Ссылка не найдена", 404)
        short_url = f"{_short_base(request)}/{code}"
        qr = _lnk.make_qr_data_uri(short_url)
        if not qr:
            return _err("QR временно недоступен")
        return _json_resp({"ok": True, "qr": qr, "short_url": short_url})

    async def links_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        lim = _list_limit(request, 100, 500)
        off = _list_offset(request)
        items = await _lnk.list_for_owner(pool, uid, limit=lim, offset=off)
        total = await _lnk.count_for_owner(pool, uid)
        base = _short_base(request)
        for it in items:
            it["short_url"] = f"{base}/{it['code']}"
        return _json_resp({"links": items, "base": base, "total": total,
                           "page": {"limit": lim, "offset": off,
                                    "has_more": off + len(items) < total}})

    async def links_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        code = validate_string(body.get("code"), max_len=40)
        ok = await _lnk.set_disabled(pool, uid, code or "", bool(body.get("disabled")))
        return _json_resp({"ok": ok}) if ok else _err("Ссылка не найдена", 404)

    async def links_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        code = validate_string(body.get("code"), max_len=40)
        ok = await _lnk.delete(pool, uid, code or "")
        return _json_resp({"ok": ok}) if ok else _err("Ссылка не найдена", 404)

    app.router.add_post("/api/miniapp/links/create", links_create)
    app.router.add_get("/api/miniapp/links", links_list)
    app.router.add_post("/api/miniapp/links/toggle", links_toggle)
    app.router.add_post("/api/miniapp/links/delete", links_delete)
    app.router.add_post("/api/miniapp/links/qr", links_qr)
