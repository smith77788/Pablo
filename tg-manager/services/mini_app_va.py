"""Виртуальный администратор канала — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 16 маршрутов `/api/miniapp/va/*` и
функции, которыми пользовались только они — стратегия сети, настройка
администратора на канале, публикация прямо сейчас, черновики (принять,
отклонить, перегенерировать), эталонные источники и откат обучения.

Пять общих помощников уехали вместе с ручками, потому что других
потребителей у них нет: `_va_cid` (идентификатор канала из пути),
`_va_body` (тело запроса), `_va_payload` (состояние администратора канала),
`_va_save` (сохранение и установка) и `_va_draft` (одно действие над
черновиком на три ручки).

Скоуп владельца здесь держится не на этом файле: канал проверяется в
`services/channel_admin.py`, обучение — в `services/va_learning.py`.
Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты виртуального администратора канала."""
    from services.mini_app_api import (
        _err, _get_uid, _json_resp, log, va_post_link,
    )

    def _va_cid(request: web.Request) -> int | None:
        try:
            return int(request.match_info["cid"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _va_body(request: web.Request) -> dict | None:
        try:
            body = await request.json()
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    async def va_channels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import channel_admin as _ca
        include_summary = "page" not in request.query or request.query.get("summary") != "0"
        try:
            if "page" in request.query:
                try:
                    page = int(request.query.get("page", "0"))
                except (TypeError, ValueError):
                    return _err("Неверный номер страницы каналов")
                try:
                    items = await _ca.list_channels_page(
                        pool, uid, page=page, page_size=30,
                        query=request.query.get("q", ""), state=request.query.get("state", "all"),
                    )
                except _ca.ChannelAdminError as exc:
                    return _err(str(exc), 400)
            else:
                items = {"channels": await _ca.list_channels(pool, uid)}
            drafts = await _ca.list_drafts(pool, uid) if include_summary else []
            network = await _ca.network_overview(pool, uid) if include_summary else None
            if network is not None:
                try:
                    from services import va_control
                    network["next_actions"] = await va_control.network_actions(pool, uid)
                except Exception:
                    log.debug("va network actions failed uid=%s", uid, exc_info=True)
                    network["next_actions"] = []
        except Exception:
            log.warning("va_channels failed uid=%s", uid, exc_info=True)
            return _err("Не удалось загрузить каналы", 500)
        return _json_resp({"ok": True, **items, "drafts": drafts, "network": network})

    async def va_posts(request: web.Request) -> web.Response:
        """История опубликованных постов виртуального администратора с переходом
        к посту В КАНАЛЕ. Жалоба владельца: «не видно, что публиковал вирт.админ,
        и нельзя нажать на пост и перейти к нему в канале».

        Ссылку строим из managed_channels: публичный канал (есть @username) →
        t.me/<username>/<msg_id>; приватный → t.me/c/<internal>/<msg_id>
        (откроется у участника). Нет msg_id (старый пост до фикса) или канал не
        сопоставлен — ссылки нет, строка это честно показывает."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT p.id, p.channel_key, p.msg_id, p.pillar, p.body,
                          p.published_at, p.op_id,
                          mc.title AS ch_title, mc.username AS ch_username,
                          mc.channel_id AS ch_id
                   FROM va_channel_posts p
                   LEFT JOIN managed_channels mc
                          ON mc.owner_id = p.owner_id
                         AND (p.channel_key = mc.channel_id::text
                              OR p.channel_key = mc.username
                              OR p.channel_key = '@' || mc.username)
                   WHERE p.owner_id = $1
                   ORDER BY p.published_at DESC, p.id DESC
                   LIMIT 50""", uid)
        except Exception:
            log.warning("va_posts failed uid=%s", uid, exc_info=True)
            return _err("Не удалось загрузить посты", 500)

        posts = []
        for r in rows or []:
            link, is_private = va_post_link(r["ch_username"], r["ch_id"], r["msg_id"])
            posts.append({
                "id": r["id"],
                "pillar": r["pillar"],
                "body": (r["body"] or "")[:280],
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                "channel_title": r["ch_title"] or r["channel_key"],
                "channel_username": r["ch_username"],
                "msg_id": r["msg_id"],
                "op_id": r["op_id"],
                "link": link,
                "is_private": is_private,
            })
        return _json_resp({"ok": True, "posts": posts})

    async def va_network_strategy_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Нет доступа", 401)
        from services import va_strategy
        return _json_resp(await va_strategy.get_strategy(pool, uid))

    async def va_network_strategy_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Нет доступа", 401)
        from services import va_strategy
        try:
            result = await va_strategy.save_strategy(pool, uid, await _va_body(request))
        except va_strategy.StrategyError as exc:
            return _err(str(exc), 400)
        return _json_resp({"ok": True, **result})

    async def _va_payload(uid: int, cid: int) -> dict | None:
        """Экран канала: настройки, рубрики, план, черновики, статистика, журнал."""
        from services import va_workspace
        return await va_workspace.load_workspace(pool, uid, cid)

    async def va_channel_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        cid = _va_cid(request)
        if cid is None:
            return _err("Неверный канал")
        data = await _va_payload(uid, cid)
        if data is None:
            return _err("Канал не найден среди ваших каналов", 404)
        return _json_resp(data)

    async def va_learning_rollback(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Нет доступа", 401)
        cid = _va_cid(request)
        if cid is None:
            return _err("Неверный канал", 400)
        from services import channel_admin as _ca
        from services import va_learning
        if not await _ca.channel_row(pool, uid, cid):
            return _err("Канал не найден среди ваших каналов", 404)
        body = await _va_body(request)
        decision_id = (body or {}).get("decision_id")
        if type(decision_id) is not int or decision_id <= 0:
            return _err("Неверное решение для отмены", 400)
        try:
            await va_learning.rollback(pool, uid, cid, decision_id)
        except va_learning.LearningConflict as exc:
            return _err(str(exc), 409)
        return await va_channel_get_for(uid, cid)

    async def _va_save(uid: int, cid: int, body: dict, *, install: bool) -> web.Response:
        from services import channel_admin as _ca
        from services import channel_brain_store as _cbs
        clean, errors = _ca.validate_settings(body)
        pillars = weights = None
        if "pillars" in body:
            pillars, weights, p_err = _cbs.validate_pillars(body.get("pillars"))
            errors += p_err
        if errors:
            return _err("; ".join(errors[:5]), 400)
        try:
            if install:
                await _ca.install(pool, uid, cid, clean)
            else:
                await _ca.save_settings(pool, uid, cid, clean)
        except _ca.ChannelAdminError as e:
            return _err(str(e), 400)
        if pillars is not None:
            prev = await _cbs.get_profile(pool, uid, str(cid))
            await _cbs.save_profile(
                pool, uid, str(cid),
                brand_rules=_ca._brand_rules_dict(prev.brand_rules) if prev else {},
                pillars=pillars, mix_weights=weights,
                autonomy_mode=prev.autonomy_mode if prev else await _ca.owner_mode(pool, uid),
                max_streak=prev.max_streak if prev else 2,
                dup_threshold=prev.dup_threshold if prev else 0.6,
            )
        return await va_channel_get_for(uid, cid)

    async def va_channel_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        cid = _va_cid(request)
        body = await _va_body(request)
        if cid is None or body is None:
            return _err("Не удалось разобрать запрос")
        return await _va_save(uid, cid, body, install=False)

    async def va_channel_install(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        cid = _va_cid(request)
        body = await _va_body(request)
        if cid is None:
            return _err("Неверный канал")
        log.info("va install uid=%s channel=%s", uid, cid)
        return await _va_save(uid, cid, body or {}, install=True)

    async def va_channel_reconfigure(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        cid = _va_cid(request)
        if cid is None:
            return _err("Неверный канал")
        from services import channel_admin as _ca
        if not await _ca.get_admin(pool, uid, cid):
            return _err("Администратор на этом канале не установлен", 404)
        await _ca.reconfigure(pool, uid, cid)
        return await va_channel_get_for(uid, cid)

    async def va_channel_post_now(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        cid = _va_cid(request)
        if cid is None:
            return _err("Неверный канал")
        from services import channel_admin as _ca
        try:
            res = await _ca.post_now(pool, None, uid, cid)
        except _ca.ChannelAdminError as e:
            return _err(str(e), 400)
        msg = {
            "published": "Пост написан и передан в очередь. Доставка будет подтверждена отдельно",
            "draft": "Пост написан — редактор нашёл замечания, он ждёт вашего решения ниже",
            "error": "Не получилось написать пост — причина в журнале, администратор повторит сам",
        }.get(res, "Готово")
        return _json_resp({"ok": res != "error", "result": res, "message": msg})

    async def _va_draft(request: web.Request, action: str) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            did = int(request.match_info["did"])
        except (KeyError, TypeError, ValueError):
            return _err("Неверный черновик")
        from services import channel_admin as _ca
        fn = {"publish": _ca.publish_draft, "reject": _ca.reject_draft,
              "regenerate": _ca.regenerate_draft}[action]
        kw = {}
        body = await _va_body(request) or {}
        if action == "publish":
            # Правка владельца перед публикацией: публикуем её вместо исходного
            # текста. Пустое поле — публикуем как есть.
            if str(body.get("body") or "").strip():
                kw["body"] = body["body"]
        else:
            kw["reason"] = body.get("reason") or ""
        try:
            res = await fn(pool, uid, did, **kw)
        except _ca.ChannelAdminError as e:
            return _err(str(e), 400)
        return _json_resp({"ok": True, **res})

    async def va_draft_publish(request: web.Request) -> web.Response:
        return await _va_draft(request, "publish")

    async def va_draft_reject(request: web.Request) -> web.Response:
        return await _va_draft(request, "reject")

    async def va_draft_regenerate(request: web.Request) -> web.Response:
        return await _va_draft(request, "regenerate")

    async def va_ref_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        cid = _va_cid(request)
        body = await _va_body(request)
        if cid is None or body is None:
            return _err("Не удалось разобрать запрос")
        from services import va_references as _var
        try:
            ref = await _var.add_ref(pool, uid, cid, body.get("ref"), body.get("kind") or "competitor",
                                     focus=body.get("focus", ""))
        except _var.ReferenceError_ as e:
            return _err(str(e), 400)
        log.info("va reference add uid=%s channel=%s ref=%s", uid, cid, ref["username"])
        await _var.analyze(pool, uid, ref["id"])
        return await va_channel_get_for(uid, cid)

    async def va_ref_refresh(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["rid"])
        except (KeyError, TypeError, ValueError):
            return _err("Неверный образец")
        row = await pool.fetchrow(
            "SELECT channel_id, attempted_at > now() - interval '10 minutes' AS fresh "
            "FROM va_reference_channels WHERE id=$1 AND owner_id=$2", rid, uid)
        if not row:
            return _err("Образец не найден", 404)
        if row["fresh"]:
            return _err("Образец только что изучался — повторить можно через 10 минут", 429)
        from services import va_references as _var
        await _var.analyze(pool, uid, rid)
        return await va_channel_get_for(uid, int(row["channel_id"]))

    async def va_ref_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["rid"])
        except (KeyError, TypeError, ValueError):
            return _err("Неверный образец")
        cid = await pool.fetchval(
            "SELECT channel_id FROM va_reference_channels WHERE id=$1 AND owner_id=$2", rid, uid)
        from services import va_references as _var
        if cid is None or not await _var.delete_ref(pool, uid, rid):
            return _err("Образец не найден", 404)
        return await va_channel_get_for(uid, int(cid))

    async def va_channel_get_for(uid: int, cid: int) -> web.Response:
        data = await _va_payload(uid, cid)
        if data is None:
            return _err("Канал не найден среди ваших каналов", 404)
        return _json_resp(data)

    app.router.add_get("/api/miniapp/va/strategy", va_network_strategy_get)
    app.router.add_put("/api/miniapp/va/strategy", va_network_strategy_save)
    app.router.add_post("/api/miniapp/va/channel/{cid}/learning/rollback", va_learning_rollback)
    app.router.add_get("/api/miniapp/va/channels", va_channels)
    app.router.add_get("/api/miniapp/va/posts", va_posts)
    app.router.add_get("/api/miniapp/va/channel/{cid}", va_channel_get)
    app.router.add_put("/api/miniapp/va/channel/{cid}", va_channel_save)
    app.router.add_post("/api/miniapp/va/channel/{cid}/install", va_channel_install)
    app.router.add_post("/api/miniapp/va/channel/{cid}/reconfigure", va_channel_reconfigure)
    app.router.add_post("/api/miniapp/va/channel/{cid}/post_now", va_channel_post_now)
    app.router.add_post("/api/miniapp/va/drafts/{did}/publish", va_draft_publish)
    app.router.add_post("/api/miniapp/va/drafts/{did}/reject", va_draft_reject)
    app.router.add_post("/api/miniapp/va/drafts/{did}/regenerate", va_draft_regenerate)
    app.router.add_post("/api/miniapp/va/channel/{cid}/references", va_ref_add)
    app.router.add_post("/api/miniapp/va/references/{rid}/refresh", va_ref_refresh)
    app.router.add_delete("/api/miniapp/va/references/{rid}", va_ref_delete)
