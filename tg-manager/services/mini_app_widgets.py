"""«🧩 Виджеты в чатах» в мини-аппе: собрать, проверить, отправить, поправить.

Логика виджетов — services/bot_widgets.py (шаблоны, проверка, вызовы Bot API)
и services/bot_widget_runtime.py (учёт отправленных и голоса); здесь только
двери к ним. Отправляет всегда бот ВЛАДЕЛЬЦА: чужой бот, выключенный бот и
чужой виджет — 404, как будто их нет.

Куда: «себе» (личка владельца с ботом — проверить вид), группа, где бот
состоит (@username или числовой id), или любой чат по id. Каналы Telegram
для виджетов закрыты — это скажет сам Telegram, и экран переведёт отказ.
"""
from __future__ import annotations

import re

from aiohttp import web

LIST_MAX = 100
# Аудитории рассылки — те же, что у рассылки текста (mini_app_broadcast).
BROADCAST_SEGMENTS = {
    "all": "",
    "active_7d": " AND last_seen >= now() - interval '7 days'",
    "active_30d": " AND last_seen >= now() - interval '30 days'",
    "inactive_14d": " AND (last_seen IS NULL OR last_seen < now() - interval '14 days')",
}
_CHAT_USERNAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")
_CHAT_ID = re.compile(r"^-?\d{1,20}$")


def parse_chat(raw, uid: int):
    """«me» | @username | числовой id → chat_id для Bot API; None — не разобрать."""
    s = str(raw if raw is not None else "").strip()
    if s in ("", "me"):
        return int(uid)
    if s.startswith("https://t.me/"):
        s = "@" + s[len("https://t.me/"):].strip("/")
    if _CHAT_USERNAME.match(s):
        return s
    if _CHAT_ID.match(s):
        return int(s)
    return None


def _size(v) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 600))


def setup_routes(app: web.Application, pool) -> None:
    import aiohttp

    from services import bot_api, broadcaster
    from services import bot_widget_runtime as rt
    from services import operation_bus as _obus
    from services import bot_widgets as bw
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
    )
    from services.security import validate_integer

    async def _widget_body(request):
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return None, (bad if bad is not None else _err("Не удалось разобрать запрос", 400))
        if not isinstance(data, dict):
            return None, _err("Не удалось разобрать запрос", 400)
        return data, None

    async def _own_bot(uid: int, bot_id) -> dict | None:
        try:
            bid = int(bot_id)
        except (TypeError, ValueError):
            return None
        row = await pool.fetchrow(
            "SELECT bot_id, token, username FROM managed_bots "
            "WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE", bid, uid)
        return dict(row) if row else None

    async def _own_widget(uid: int, wid) -> dict | None:
        try:
            w = int(wid)
        except (TypeError, ValueError):
            return None
        row = await pool.fetchrow(
            """SELECT w.*, mb.token, mb.username AS bot_username, mb.is_active AS bot_active
                 FROM bot_widgets w JOIN managed_bots mb ON mb.bot_id=w.bot_id
                WHERE w.id=$1 AND w.owner_id=$2 AND mb.added_by=$2""", w, uid)
        return dict(row) if row else None

    def _view(row: dict, votes: int = 0) -> dict:
        data = bw.load_data(row.get("public_data"))
        return {
            "id": row["id"], "bot_id": row["bot_id"],
            "bot_username": row.get("bot_username") or "",
            "chat_id": row["chat_id"], "message_id": row["message_id"],
            "template": row["template"],
            "template_name": (bw.TEMPLATES.get(row["template"]) or {}).get("name") or "Свой код",
            "title": row["title"], "data": data,
            "votes": int(votes or 0), "shared": bool(row.get("widget_id")),
            "editable": row["template"] in bw.TEMPLATES,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    async def widget_templates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        items = bw.templates_public()
        for t in items:
            t["html"] = bw.template_html(t["id"])
        return _json_resp({"templates": items, "limits": {
            "html_max": bw.HTML_MAX, "packed_max": bw.PACKED_MAX,
            "packed_warn": bw.PACKED_WARN, "data_max": bw.DATA_MAX}})

    async def widget_check(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _widget_body(request)
        if bad is not None:
            return bad
        html = data.get("html")
        if not isinstance(html, str) or len(html) > bw.HTML_MAX * 4:
            return _err("Код виджета — текст до 32 КБ", 400)
        return _json_resp(bw.check(html))

    async def widget_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.rel_url.query
        limit = validate_integer(q.get("limit", "30"), min_val=1, max_val=LIST_MAX) or 30
        offset = validate_integer(q.get("offset", "0"), min_val=0) or 0
        try:
            rows = await pool.fetch(
                """SELECT w.*, mb.username AS bot_username,
                          (SELECT COUNT(*) FROM bot_widget_votes v WHERE v.widget_row=w.id) AS votes
                     FROM bot_widgets w JOIN managed_bots mb ON mb.bot_id=w.bot_id
                    WHERE w.owner_id=$1 AND mb.added_by=$1
                    ORDER BY w.created_at DESC, w.id DESC LIMIT $2 OFFSET $3""",
                uid, limit, offset)
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM bot_widgets w JOIN managed_bots mb ON mb.bot_id=w.bot_id "
                "WHERE w.owner_id=$1 AND mb.added_by=$1", uid)
        except Exception:
            log.exception("widget_list uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        items = [_view(dict(r), r["votes"]) for r in rows]
        return _json_resp({"items": items, "total": int(total or 0),
                           "has_more": offset + len(items) < int(total or 0)})

    async def widget_send(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _widget_body(request)
        if bad is not None:
            return bad
        bot = await _own_bot(uid, data.get("bot_id"))
        if not bot:
            return _err("Бот не найден или выключен", 404)
        chat_id = parse_chat(data.get("chat"), uid)
        if chat_id is None:
            return _err("Куда отправить: «себе», @username группы или её числовой id", 400)
        template = str(data.get("template") or "")
        caption = str(data.get("caption") or "").strip()[:bw.CAPTION_MAX]
        try:
            if template == "html":
                html = data.get("html")
                if not isinstance(html, str):
                    return _err("Вставьте код виджета", 400)
                public = str(data.get("public_data") or "")
                title = caption[:120] or "Свой виджет"
                allow_queries = False
            else:
                html = bw.template_html(template)
                if not html:
                    return _err("Такого шаблона нет", 400)
                fields = bw.build_data(template, data.get("values") or {})
                public = bw.dump_data(fields)
                title = bw.title_of(template, fields)
                allow_queries = bw.is_vote_template(template)
        except bw.WidgetError as exc:
            return _err(str(exc), 400)
        rep = bw.check(html)
        if not rep["ok"]:
            return _err(rep["errors"][0], 400)
        err = bw.data_error(public)
        if err:
            return _err(err, 400)
        try:
            async with aiohttp.ClientSession() as sess:
                res = await bw.send(sess, bot["token"], chat_id, html,
                                    width=_size(data.get("width")),
                                    height=_size(data.get("height")),
                                    caption=caption or None, allow_queries=allow_queries,
                                    is_shared=True, public_data=public or None)
        except Exception:
            log.exception("widget_send uid=%s bot=%s", uid, bot["bot_id"])
            return _err("Не удалось связаться с Telegram — повторите позже", 502)
        if not res.get("ok"):
            return _err(res.get("error") or "Telegram не принял виджет", 400)
        wid = None
        if res.get("message_id") is not None:
            try:
                wid = await rt.record_sent(
                    pool, owner_id=uid, bot_id=bot["bot_id"], chat_id=int(res["chat_id"]),
                    message_id=int(res["message_id"]), widget_id=res.get("widget_id"),
                    template=template, title=title, public_data=public,
                    allow_queries=allow_queries)
            except Exception:
                # Виджет уже в чате — не врём, что не ушёл; но голоса и правка
                # без строки работать не будут, и это надо сказать.
                log.exception("widget_send: не записан uid=%s bot=%s", uid, bot["bot_id"])
                return _json_resp({"ok": True, "id": None, "warning":
                                   "Виджет отправлен, но не сохранён в списке: голоса и "
                                   "правка для него работать не будут."})
        return _json_resp({"ok": True, "id": wid, "message_id": res.get("message_id"),
                           "chat_id": res.get("chat_id"), "warnings": rep["warnings"]})

    async def widget_edit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _widget_body(request)
        if bad is not None:
            return bad
        row = await _own_widget(uid, request.match_info.get("wid"))
        if not row:
            return _err("Виджет не найден", 404)
        if not row["bot_active"]:
            return _err("Бот выключен — включите его, чтобы править виджет", 409)
        tpl = row["template"]
        if tpl not in bw.TEMPLATES:
            return _err("Код своего виджета после отправки не меняется — отправьте новый", 400)
        try:
            fresh = bw.build_data(tpl, data.get("values") or {})
        except bw.WidgetError as exc:
            return _err(str(exc), 400)
        # Голоса берутся из базы под замком строки: касание, пришедшее
        # во время правки, не потеряется и не откатит новые тексты.
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(
                    "SELECT public_data, revision FROM bot_widgets WHERE id=$1 AND owner_id=$2 "
                    "FOR UPDATE", row["id"], uid)
                if not cur:
                    return _err("Виджет не найден", 404)
                old = bw.load_data(cur["public_data"])
                try:
                    merged = bw.merge_edit(tpl, old, fresh)
                except bw.WidgetError as exc:
                    return _err(str(exc), 400)
                text = bw.dump_data(merged)
                err = bw.data_error(text)
                if err:
                    return _err(err, 400)
                rev = int(cur["revision"] or 0) + 1
                await conn.execute(
                    "UPDATE bot_widgets SET public_data=$2, title=$3, revision=$4, updated_at=NOW() "
                    "WHERE id=$1", row["id"], text, bw.title_of(tpl, merged), rev)
        try:
            async with aiohttp.ClientSession() as sess:
                res = await bw.edit_data(sess, row["token"], chat_id=row["chat_id"],
                                         message_id=row["message_id"],
                                         widget_id=row.get("widget_id"), public_data=text)
        except Exception:
            log.exception("widget_edit uid=%s wid=%s", uid, row["id"])
            res = {"ok": False, "error": "Не удалось связаться с Telegram — повторите позже"}
        if not res.get("ok"):
            # Telegram правку не принял — возвращаем прежние данные, если за это
            # время не было голосов (иначе новые тексты уже ушли с голосом).
            await pool.execute(
                "UPDATE bot_widgets SET public_data=$2, title=$3, updated_at=NOW() "
                "WHERE id=$1 AND revision=$4",
                row["id"], row["public_data"], row["title"], rev)
            return _err(res.get("error") or "Telegram не принял правку", 400)
        return _json_resp({"ok": True})

    async def widget_broadcast(request: web.Request) -> web.Response:
        """Разослать общий виджет подписчикам его бота — обычной рассылкой бота.

        Рассылка та же, что у текста (run_broadcast: журнал доставок, стоп,
        докатка после рестарта, лимиты Telegram), только каждому уходит
        sendSharedWidget: у всех получателей один счёт голосов. Признак
        виджета пишется тем же INSERT, что и строка рассылки.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _widget_body(request)
        if bad is not None:
            return bad
        row = await _own_widget(uid, request.match_info.get("wid"))
        if not row:
            return _err("Виджет не найден", 404)
        if not row["bot_active"]:
            return _err("Бот выключен — включите его, чтобы разослать виджет", 409)
        if not row.get("widget_id"):
            return _err("Этот виджет отправлен не общим — разослать можно только общий. "
                        "Отправьте его заново.", 400)
        segment = str(data.get("segment") or "all")
        seg_sql = BROADCAST_SEGMENTS.get(segment)
        if seg_sql is None:
            return _err("Неизвестная аудитория рассылки", 400)
        try:
            total = int(await pool.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true "
                "AND is_blocked=false AND suspect=false" + seg_sql, row["bot_id"]) or 0)
        except Exception:
            log.exception("widget_broadcast count uid=%s wid=%s", uid, row["id"])
            return _err(_INTERNAL_ERROR, 500)
        if not total:
            return _err("Некому отправлять: у бота нет активных подписчиков в этой аудитории", 400)
        text = f"🧩 Виджет: {row['title']}"[:200]
        try:
            bc_id = await pool.fetchval(
                "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, widget_id) "
                "VALUES($1,$2,$3,'pending',$4,$5) RETURNING id",
                row["bot_id"], text, total, uid, row["widget_id"])
        except Exception:
            log.exception("widget_broadcast insert uid=%s wid=%s", uid, row["id"])
            return _err("Не удалось создать рассылку", 500)
        params = {"bot_id": row["bot_id"], "text": text, "broadcast_id": int(bc_id),
                  "widget_id": row["widget_id"]}
        if seg_sql:
            params["segment"] = segment
        label = f"Рассылка виджета @{row.get('bot_username') or row['bot_id']}: {row['title'][:40]}"
        try:
            op_id = await _obus.submit(pool, uid, "run_broadcast", params,
                                       total_items=total, label=label)
        except PermissionError as exc:
            await broadcaster.abandon_unstarted(pool, bc_id)
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            # Строка рассылки уже в 'pending' — без снятия её подняла бы
            # докатка на старте процесса мимо отказа постановки.
            await broadcaster.abandon_unstarted(pool, bc_id)
            log.exception("widget_broadcast submit uid=%s wid=%s", uid, row["id"])
            return _err("Не удалось поставить рассылку в очередь", 500)
        return _json_resp({"ok": True, "op_id": op_id, "broadcast_id": bc_id, "total": total})

    async def widget_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _widget_body(request)
        if bad is not None:
            return bad
        row = await _own_widget(uid, request.match_info.get("wid"))
        if not row:
            return _err("Виджет не найден", 404)
        forget_only = bool(data.get("forget_only"))
        if not forget_only:
            try:
                async with aiohttp.ClientSession() as sess:
                    res = await bot_api._call(sess, row["token"], "deleteMessage",
                                              chat_id=row["chat_id"],
                                              message_id=row["message_id"])
            except Exception:
                log.exception("widget_delete uid=%s wid=%s", uid, row["id"])
                return _err("Не удалось связаться с Telegram — повторите позже", 502)
            desc = str(res.get("description") or "").lower()
            if not res.get("ok") and "not found" not in desc:
                return _err("Telegram не дал удалить сообщение (в группах бот удаляет только "
                            "свежие сообщения). Можно убрать виджет только из списка.", 409)
        await pool.execute("DELETE FROM bot_widgets WHERE id=$1 AND owner_id=$2", row["id"], uid)
        return _json_resp({"ok": True})

    app.router.add_get("/api/miniapp/widgets/templates", widget_templates)
    app.router.add_post("/api/miniapp/widgets/check", widget_check)
    app.router.add_get("/api/miniapp/widgets", widget_list)
    app.router.add_post("/api/miniapp/widgets/send", widget_send)
    app.router.add_post("/api/miniapp/widgets/{wid}/edit", widget_edit)
    app.router.add_post("/api/miniapp/widgets/{wid}/broadcast", widget_broadcast)
    app.router.add_post("/api/miniapp/widgets/{wid}/delete", widget_delete)
