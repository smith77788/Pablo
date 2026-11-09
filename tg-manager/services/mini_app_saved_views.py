"""Сохранённые виды списков ресурсов (schema_v314, saved_views).

Вид — набор фильтров, поиска и сортировки списка под именем: «Мёртвые RU»,
«Новые на прогреве», «Прокси без аккаунтов». При сотнях и тысячах ресурсов
один и тот же срез собирают по нескольку раз в день, и каждый раз — тапами
по чипам заново. Вид открывает его одним тапом.

Сервер параметры вида не толкует: это словарь, который экран сам
применяет к своему списку. Здесь — только владелец, вид списка, имя и
ограничение размера, чтобы в таблицу нельзя было сложить произвольный груз.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

log = logging.getLogger(__name__)

KINDS = ("accounts", "bots", "channels", "proxies")
MAX_VIEWS = 30           # на вид списка у одного владельца
MAX_PARAMS_BYTES = 2000
NAME_MAX = 40


def clean_params(raw) -> dict | None:
    """Параметры вида: плоский словарь строк, чисел и булевых значений."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, v in list(raw.items())[:20]:
        if not isinstance(k, str) or len(k) > 32:
            continue
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:100]
    if len(json.dumps(out, ensure_ascii=False)) > MAX_PARAMS_BYTES:
        return None
    return out


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _INTERNAL_ERROR, _err, _get_uid, _json_resp

    async def views_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        kind = request.query.get("kind")
        if kind not in KINDS:
            return _err("Неизвестный список", 400)
        try:
            rows = await pool.fetch(
                "SELECT id, name, params FROM saved_views WHERE owner_id=$1 AND kind=$2 "
                "ORDER BY created_at, id", uid, kind)
        except Exception:
            log.debug("saved_views: список не прочитан uid=%s", uid, exc_info=True)
            rows = []
        out = []
        for r in rows:
            p = r["params"]
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except ValueError:
                    p = {}
            out.append({"id": int(r["id"]), "name": r["name"], "params": p or {}})
        return _json_resp({"views": out})

    async def views_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        kind = body.get("kind")
        name = str(body.get("name") or "").strip()[:NAME_MAX]
        params = clean_params(body.get("params"))
        if kind not in KINDS:
            return _err("Неизвестный список", 400)
        if not name:
            return _err("Назовите вид", 400)
        if params is None:
            return _err("Слишком большой вид", 400)
        try:
            n = await pool.fetchval(
                "SELECT COUNT(*) FROM saved_views WHERE owner_id=$1 AND kind=$2 AND name<>$3",
                uid, kind, name)
            if int(n or 0) >= MAX_VIEWS:
                return _err(f"Не больше {MAX_VIEWS} видов на список", 400)
            vid = await pool.fetchval(
                "INSERT INTO saved_views(owner_id, kind, name, params) VALUES ($1,$2,$3,$4::jsonb) "
                "ON CONFLICT (owner_id, kind, name) DO UPDATE SET params=EXCLUDED.params "
                "RETURNING id", uid, kind, name, json.dumps(params, ensure_ascii=False))
        except Exception:
            log.exception("saved_views: не сохранён uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "id": int(vid), "name": name, "params": params})

    async def views_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            vid = int(request.match_info["view_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        try:
            res = await pool.execute(
                "DELETE FROM saved_views WHERE id=$1 AND owner_id=$2", vid, uid)
        except Exception:
            log.exception("saved_views: не удалён uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if str(res).endswith(" 0"):
            return _err("Вид не найден", 404)
        return _json_resp({"ok": True})

    app.router.add_get("/api/miniapp/views", views_list)
    app.router.add_post("/api/miniapp/views", views_save)
    app.router.add_delete("/api/miniapp/views/{view_id}", views_delete)
