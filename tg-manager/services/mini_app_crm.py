"""Единый инбокс и воронка денег — маршруты мини-аппа.

* `/api/miniapp/inbox*` — разговоры со всех аккаунтов, Хранилища, релея и
  ботов-продавцов одним списком (services/unified_inbox.py) и ответ через
  релей бота;
* `/api/miniapp/money_funnel*` — охват → подписка → ответ → покупка по
  кампаниям, расход и стоимость лида (services/money_funnel.py).

Всё скоупится по владельцу из сессии: чужой peer_user_id даёт пустую ленту,
а не чужую переписку, потому что каждая выборка фильтруется owner_id.
"""
from __future__ import annotations

import html as _html

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    from services import money_funnel as _mf
    from services import unified_inbox as _ui
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
    )

    def _peer(request):
        try:
            v = int(request.match_info["peer_user_id"])
        except (KeyError, ValueError):
            return None
        return v if v > 0 else None

    async def inbox_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        waiting = request.query.get("waiting") in ("1", "true", "yes")
        try:
            limit = max(1, min(int(request.query.get("limit") or 100), 300))
        except ValueError:
            limit = 100
        try:
            return _json_resp(await _ui.list_threads(
                pool, uid, only_waiting=waiting, limit=limit))
        except Exception:
            log.exception("inbox_list uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def inbox_thread(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        pid = _peer(request)
        if not pid:
            return _err("Неверный собеседник", 400)
        try:
            return _json_resp(await _ui.get_thread(pool, uid, pid))
        except Exception:
            log.exception("inbox_thread uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def inbox_read(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        pid = _peer(request)
        if not pid:
            return _err("Неверный собеседник", 400)
        try:
            return _json_resp({"ok": True, "marked": await _ui.mark_read(pool, uid, pid)})
        except Exception:
            log.exception("inbox_read uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def inbox_reply(request: web.Request) -> web.Response:
        """Ответить человеку через релей бота, которому он написал.

        Только релей: ответ от имени аккаунта флота требует захвата сессии,
        которую в этот момент держит слушатель, а второй коннект на ту же
        сессию — AUTH_KEY_DUPLICATED. Для аккаунтов экран даёт ссылку в Telegram.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        pid = _peer(request)
        if not pid:
            return _err("Неверный собеседник", 400)
        try:
            body = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return bad if bad is not None else _err("Не удалось разобрать запрос", 400)
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return _err("Пустой ответ", 400)
        if len(text) > 4000:
            return _err("Слишком длинный ответ (больше 4000 знаков)", 400)
        try:
            bot_id = int((body or {}).get("bot_id") or 0)
        except (TypeError, ValueError):
            bot_id = 0
        try:
            from database import db as _db
            sess = await _db.fetchrow_bot(pool,
                """SELECT rs.id, rs.user_id, mb.token
                     FROM relay_sessions rs
                     JOIN managed_bots mb ON mb.bot_id = rs.bot_id
                    WHERE rs.user_id=$1 AND mb.added_by=$2
                      AND ($3::bigint = 0 OR rs.bot_id = $3)
                    ORDER BY rs.last_activity DESC LIMIT 1""", pid, uid, bot_id)
            if not sess:
                return _err("Этому человеку можно ответить только в Telegram: "
                            "через релей бота он не писал", 404)
            import aiohttp as _aiohttp
            from services import bot_api as _bapi
            async with _aiohttp.ClientSession() as http:
                # Бот шлёт с parse_mode=HTML: текст владельца экранируем, иначе
                # «<» в ответе ломает отправку целиком.
                ok, _ = await _bapi.send_message(http, sess["token"], sess["user_id"],
                                                 _html.escape(text))
            if not ok:
                return _err("Не удалось отправить: человек мог заблокировать бота", 502)
            await _db.save_relay_message(pool, sess["id"], "out", text)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("inbox_reply uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def money_funnel_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.query.get("days") or 30)
        except ValueError:
            days = 30
        try:
            return _json_resp(await _mf.build(pool, uid, days))
        except Exception:
            log.exception("money_funnel uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def money_funnel_spend(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return bad if bad is not None else _err("Не удалось разобрать запрос", 400)
        try:
            cid = int((body or {}).get("campaign_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0:
            return _err("Не выбрана кампания", 400)
        try:
            res = await _mf.set_spend(pool, uid, cid, (body or {}).get("amount"),
                                      (body or {}).get("currency") or "RUB")
            return _json_resp({"ok": True, **res})
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("money_funnel_spend uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/inbox", inbox_list)
    app.router.add_get("/api/miniapp/inbox/{peer_user_id}", inbox_thread)
    app.router.add_post("/api/miniapp/inbox/{peer_user_id}/read", inbox_read)
    app.router.add_post("/api/miniapp/inbox/{peer_user_id}/reply", inbox_reply)
    app.router.add_get("/api/miniapp/money_funnel", money_funnel_get)
    app.router.add_post("/api/miniapp/money_funnel/spend", money_funnel_spend)
