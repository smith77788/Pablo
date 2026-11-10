"""«📣 Самопиар» в мини-аппе: кампании по своим каналам и ботам.

Логика — services/self_promo.py; здесь только двери. Всё, что приходит в
запросе (боты, каналы, id кампании), сверяется с владельцем запросом к базе:
чужое — «не найдено».
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    from services import self_promo as sp
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
    )

    async def _promo_body(request):
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return None, (bad if bad is not None else _err("Не удалось разобрать запрос", 400))
        if not isinstance(data, dict):
            return None, _err("Не удалось разобрать запрос", 400)
        return data, None

    async def _own_campaign(uid: int, cid) -> dict | None:
        return await sp.get_campaign(pool, uid, cid)

    def _counts_ru(counts: dict, channel_posts: int) -> dict:
        return {**counts, "channel_posts": int(channel_posts)}

    async def selfpromo_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await sp.overview(pool, uid)
            data["campaigns"] = await sp.list_campaigns(pool, uid)
            data["limits"] = {"title": sp.TITLE_MAX, "body": sp.BODY_MAX,
                              "button": sp.BUTTON_MAX, "per_launch": sp.MAX_PER_LAUNCH}
        except Exception:
            log.exception("selfpromo_overview uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp(data)

    async def _resolved(uid: int, data: dict) -> dict:
        return await sp.resolve_campaign(pool, uid, sp.clean_campaign(data))

    async def selfpromo_preview(request: web.Request) -> web.Response:
        """Скольким людям уйдёт — ДО сохранения и запуска, с причинами отсева."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _promo_body(request)
        if bad is not None:
            return bad
        try:
            c = await _resolved(uid, data)
            if data.get("id"):
                own = await _own_campaign(uid, data.get("id"))
                if not own:
                    return _err("Кампания не найдена", 404)
                c["id"] = own["id"]
            aud = await sp.audience(pool, uid, c)
        except sp.PromoError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("selfpromo_preview uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        text, buttons = sp.message_for(c)
        return _json_resp({"counts": _counts_ru(aud["counts"], len(c["channel_ids"])),
                           "per_bot": {str(k): len(v) for k, v in aud["send"].items()},
                           "target_name": c["target_name"], "title": c["title"],
                           "link": sp.link_for(c), "has_button": bool(buttons)})

    async def selfpromo_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _promo_body(request)
        if bad is not None:
            return bad
        try:
            c = await _resolved(uid, data)
            cid = await sp.save_campaign(pool, uid, c, data.get("id") or None)
        except sp.PromoError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("selfpromo_save uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "id": cid})

    async def selfpromo_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        c = await _own_campaign(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        if c["status"] == "archived":
            return _err("Кампания в архиве — верните её, чтобы запустить", 409)
        try:
            # Боты и каналы могли исчезнуть или выключиться после сохранения.
            await sp.resolve_campaign(pool, uid, c)
            res = await sp.launch(pool, uid, c, trigger="manual")
        except sp.PromoError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("selfpromo_launch uid=%s cid=%s", uid, c["id"])
            return _err(_INTERNAL_ERROR, 500)
        if res.get("refused") and not res["ok"]:
            return _err(res["refused"], 403)
        if not res["ok"]:
            return _err("Отправлять некому: все, кто подходит, эту кампанию уже получили "
                        "или недавно получали другой самопиар.", 400)
        return _json_resp(res)

    async def selfpromo_test(request: web.Request) -> web.Response:
        """Пробное сообщение себе — посмотреть, как оно выглядит в личке."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _promo_body(request)
        if bad is not None:
            return bad
        try:
            c = await _resolved(uid, data)
            if data.get("id"):
                own = await _own_campaign(uid, data.get("id"))
                if own:
                    c["id"] = own["id"]
            res = await sp.test_send(pool, uid, c)
        except sp.PromoError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("selfpromo_test uid=%s", uid)
            return _err("Не удалось связаться с Telegram — повторите позже", 424)
        if not res["ok"]:
            return _err(res["error"], 400)
        return _json_resp(res)

    async def selfpromo_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _promo_body(request)
        if bad is not None:
            return bad
        c = await _own_campaign(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        try:
            await sp.set_status(pool, uid, c["id"], str(data.get("status") or ""))
        except sp.PromoError as exc:
            return _err(str(exc), 400)
        return _json_resp({"ok": True})

    async def selfpromo_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        c = await _own_campaign(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        await sp.delete_campaign(pool, uid, c["id"])
        return _json_resp({"ok": True})

    async def selfpromo_texts(request: web.Request) -> web.Response:
        """Варианты текста: три от ИИ (если подключён) и готовые заготовки."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _promo_body(request)
        if bad is not None:
            return bad
        kind = str(data.get("target_kind") or "")
        if kind not in sp.TARGET_KINDS:
            return _err("Сначала выберите, что продвигаем", 400)
        name, about = str(data.get("target_url") or "")[:80], ""
        try:
            ref = int(data.get("target_ref") or 0)
        except (TypeError, ValueError):
            ref = 0
        if kind == "bot" and ref:
            row = await pool.fetchrow(
                "SELECT username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                ref, uid)
            if not row:
                return _err("Бот не найден", 404)
            name, about = "@" + (row["username"] or ""), row["first_name"] or ""
        elif kind == "channel" and ref:
            row = await pool.fetchrow(
                "SELECT title, username, about FROM managed_channels "
                "WHERE channel_id=$1 AND owner_id=$2 ORDER BY id LIMIT 1", ref, uid)
            if not row:
                return _err("Канал не найден", 404)
            name = row["title"] or ("@" + (row["username"] or ""))
            about = row["about"] or ""
        ai = []
        if data.get("ai"):
            ai = await sp.ai_texts(uid, kind, name, about)
        return _json_resp({"ai": ai, "presets": sp.presets_for(kind, name),
                           "ai_asked": bool(data.get("ai"))})

    app.router.add_get("/api/miniapp/selfpromo/overview", selfpromo_overview)
    app.router.add_post("/api/miniapp/selfpromo/preview", selfpromo_preview)
    app.router.add_post("/api/miniapp/selfpromo/campaign", selfpromo_save)
    app.router.add_post("/api/miniapp/selfpromo/test", selfpromo_test)
    app.router.add_post("/api/miniapp/selfpromo/texts", selfpromo_texts)
    app.router.add_post("/api/miniapp/selfpromo/campaign/{cid}/launch", selfpromo_launch)
    app.router.add_post("/api/miniapp/selfpromo/campaign/{cid}/status", selfpromo_status)
    app.router.add_post("/api/miniapp/selfpromo/campaign/{cid}/delete", selfpromo_delete)
