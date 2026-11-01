"""Менеджер по продажам — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 24 маршрута `/api/miniapp/sales/*` и
25 функций — персона бота-продавца, её товары, база знаний (FAQ), эталонные
примеры ответов, способы доставки, промокоды и заказы.

Весь скоуп владельца здесь держится на `_sp_owned`: персона читается по id из
пути и отдаётся только своему владельцу, а всё, что к ней цепляется (товар,
FAQ, пример, доставка, промокод), проверяется ЧЕРЕЗ персону — иначе чужой
владелец мог бы подвесить свой ответ к чужому боту-продавцу, и тот отвечал бы
им покупателям. Удаления и правки дополнительно скоупятся по owner_id уже в
`services/bot_sales_persona.py`, то есть защита стоит в два слоя.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты менеджера по продажам на приложении мини-аппа."""
    from services import bot_sales_persona as _bsp
    from services import provider_marketplace as _mp
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _list_offset,
        int_field, log,
    )

    async def _sp_owned(uid, pid):
        p = await _bsp.get_persona(pool, pid)
        return p if (p and int(p["owner_id"]) == int(uid)) else None

    async def sp_list(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        return _json_resp({"personas": await _bsp.list_personas(pool, uid)})

    async def sp_create(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            d = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        try:
            name = d.pop("name", "")
            p = await _bsp.create_persona(pool, uid, name, **d)
            return _json_resp({"ok": True, "persona": p})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_detail(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        p = await _sp_owned(uid, pid)
        if not p:
            return _err("Не найдено", 404)
        return _json_resp({"persona": p,
                           "products": await _bsp.list_products(pool, pid),
                           "faqs": await _bsp.list_faq(pool, pid),
                           "examples": await _bsp.list_examples(pool, pid),
                           "delivery": await _bsp.list_delivery(pool, pid),
                           "promos": await _bsp.list_promos(pool, pid)})

    async def sp_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        try:
            return _json_resp({"ok": True,
                               "persona": await _bsp.update_persona(pool, pid, uid, **d)})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_update uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.delete_persona(pool, pid, uid)})

    async def sp_assign(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
            bot_id = int(d["bot_id"])
        except Exception:
            return _err("Неверный запрос", 400)
        # бот должен принадлежать пользователю (added_by)
        own = await pool.fetchval(
            "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not own:
            return _err("бот не найден или не ваш", 403)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        r = await _bsp.assign_to_bot(pool, pid, uid, bot_id)
        return _json_resp({"ok": bool(r), "persona": r})

    async def sp_unassign(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.unassign_from_bot(pool, pid, uid)})

    async def sp_product_add(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        try:
            price_cents = (int_field(d["price_cents"], "Цена")
                           if d.get("price_cents") is not None
                           else _mp.to_cents(d.get("price", "0")))
            prod = await _bsp.add_product(
                pool, pid, uid, d.get("name", ""), description=d.get("description", ""),
                sku=d.get("sku", ""), price_cents=price_cents,
                currency=d.get("currency", "USD"), in_stock=bool(d.get("in_stock", True)),
                min_qty=int_field(d.get("min_qty"), "Минимальное количество", default=1),
                unit=d.get("unit", "шт") or "шт",
                stock_qty=d.get("stock_qty"), related_skus=d.get("related_skus", "") or "",
                variants=d.get("variants") or [],
                attributes=d.get("attributes") or {})
            return _json_resp({"ok": True, "product": prod})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_product_add uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_product_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            prid = int(request.match_info["prid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        try:
            if d.get("price") is not None and d.get("price_cents") is None:
                d["price_cents"] = _mp.to_cents(d.pop("price"))
            r = await _bsp.update_product(pool, prid, uid, **d)
            if not r:
                return _err("Не найдено", 404)
            return _json_resp({"ok": True, "product": r})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_product_update uid=%s prid=%s", uid, prid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_product_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            prid = int(request.match_info["prid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.delete_product(pool, prid, uid)})

    async def sp_test(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        p = await _sp_owned(uid, pid)
        if not p:
            return _err("Не найдено", 404)
        products = await _bsp.list_products(pool, pid, active_only=True)
        return _json_resp(await _bsp.diagnose_generation(p, products))

    async def sp_stats(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        return _json_resp({"stats": await _bsp.persona_stats(pool, pid, uid)})

    async def sp_chat(request):
        """Песочница: владелец тестирует бота, история — на клиенте."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        p = await _sp_owned(uid, pid)
        if not p:
            return _err("Не найдено", 404)
        text = (d.get("text") or "").strip()
        if not text:
            return _err("пустое сообщение", 400)
        try:
            reply = await _bsp.sandbox_reply(pool, p, text, d.get("history") or [])
            return _json_resp({"ok": True, "reply": reply})
        except Exception:
            log.exception("sp_chat uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_faq_add(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        try:
            faq = await _bsp.add_faq(
                pool, pid, uid, d.get("question", ""), d.get("answer", ""),
                keywords=d.get("keywords", "") or "",
                priority=int_field(d.get("priority"), "Приоритет", default=0))
            return _json_resp({"ok": True, "faq": faq})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_faq_add uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_faq_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["fid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        r = await _bsp.update_faq(pool, fid, uid, **d)
        if not r:
            return _err("Не найдено", 404)
        return _json_resp({"ok": True, "faq": r})

    async def sp_faq_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["fid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.delete_faq(pool, fid, uid)})

    async def sp_example_add(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        try:
            ex = await _bsp.add_example(
                pool, pid, uid, d.get("user_msg", ""), d.get("assistant_msg", ""),
                ord=int_field(d.get("ord"), "Порядок", default=0))
            return _json_resp({"ok": True, "example": ex})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_example_add uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_example_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eid = int(request.match_info["eid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.delete_example(pool, eid, uid)})

    async def sp_delivery_add(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        try:
            price_cents = (int_field(d["price_cents"], "Стоимость доставки")
                           if d.get("price_cents") is not None
                           else _mp.to_cents(d.get("price", "0")))
            r = await _bsp.add_delivery(
                pool, pid, uid, d.get("name", ""), price_cents=price_cents,
                eta=d.get("eta", "") or "", zones=d.get("zones", "") or "")
            return _json_resp({"ok": True, "delivery": r})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_delivery_add uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_delivery_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            did = int(request.match_info["did"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.delete_delivery(pool, did, uid)})

    async def sp_promo_add(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _sp_owned(uid, pid):
            return _err("Не найдено", 404)
        try:
            min_total = (int_field(d["min_total_cents"], "Минимальная сумма заказа")
                         if d.get("min_total_cents") is not None
                         else _mp.to_cents(d.get("min_total", "0")))
            r = await _bsp.add_promo(
                pool, pid, uid, d.get("code", ""),
                int_field(d.get("percent"), "Процент скидки", default=0),
                min_total_cents=min_total)
            return _json_resp({"ok": True, "promo": r})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sp_promo_add uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def sp_promo_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            prid = int(request.match_info["prid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        return _json_resp({"ok": await _bsp.delete_promo(pool, prid, uid)})

    async def sp_orders(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.query
        try:
            bid = int(q["bot_id"]) if q.get("bot_id") else None
        except ValueError:
            bid = None
        _st = q.get("status") or None
        lim = _list_limit(request, 100, 500)
        off = _list_offset(request)
        orders = await _bsp.list_orders(pool, uid, bot_id=bid, status=_st,
                                        limit=lim, offset=off)
        total = await _bsp.count_orders(pool, uid, bot_id=bid, status=_st)
        return _json_resp({"orders": orders, "total": total,
                           "page": {"limit": lim, "offset": off,
                                    "has_more": off + len(orders) < total}})

    async def sp_order_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            oid = int(request.match_info["oid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        r = await _bsp.update_order(pool, oid, uid, **d)
        if not r:
            return _err("Не найдено", 404)
        return _json_resp({"ok": True, "order": r})

    # ── Качество ответов ИИ: журнал, оценка владельца, сводка и расход ──────
    from services import ai_gateway as _gw
    from services import ai_quality as _aq

    async def ai_journal(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        only = request.rel_url.query.get("only") or None
        if only not in (None, "bad", "guard"):
            return _err("Фильтр журнала: bad, guard или без фильтра", 400)
        page = await _aq.list_replies(pool, uid, limit=_list_limit(request, 30, 100),
                                      offset=_list_offset(request), only=only)
        # Итог и остаток — в ответе явно: экран пишет «Показать ещё · N из M».
        return _json_resp({"items": page["items"], "total": page["total"],
                           "offset": page["offset"], "remaining": page["remaining"]})

    async def ai_journal_rate(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["rid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        rating = d.get("rating") if isinstance(d, dict) else "нет"
        if rating not in (1, -1, None):
            return _err("Оценка: 1 — хорошо, -1 — плохо, пусто — снять", 400)
        if not await _aq.rate_reply(pool, uid, rid, rating):
            return _err("Не найдено", 404)
        return _json_resp({"ok": True, "id": rid, "rating": rating})

    async def ai_quality(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.rel_url.query.get("days") or 7)
        except ValueError:
            return _err("Период в днях — целое число", 400)
        return _json_resp(await _aq.summary(pool, uid, days=days))

    async def ai_usage(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            days = int(request.rel_url.query.get("days") or 30)
        except ValueError:
            return _err("Период в днях — целое число", 400)
        rows = await _gw.owner_usage(pool, uid, days=days)
        for r in rows:
            r["day"] = r["day"].isoformat()
        return _json_resp({"days": rows})

    app.router.add_get("/api/miniapp/sales/ai-journal", ai_journal)
    app.router.add_post("/api/miniapp/sales/ai-journal/{rid}/rate", ai_journal_rate)
    app.router.add_get("/api/miniapp/sales/ai-quality", ai_quality)
    app.router.add_get("/api/miniapp/sales/ai-usage", ai_usage)
    app.router.add_get("/api/miniapp/sales/personas", sp_list)
    app.router.add_post("/api/miniapp/sales/persona", sp_create)
    app.router.add_get("/api/miniapp/sales/persona/{pid}", sp_detail)
    app.router.add_patch("/api/miniapp/sales/persona/{pid}", sp_update)
    app.router.add_delete("/api/miniapp/sales/persona/{pid}", sp_delete)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/assign", sp_assign)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/unassign", sp_unassign)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/product", sp_product_add)
    app.router.add_patch("/api/miniapp/sales/product/{prid}", sp_product_update)
    app.router.add_delete("/api/miniapp/sales/product/{prid}", sp_product_delete)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/test", sp_test)
    app.router.add_get("/api/miniapp/sales/persona/{pid}/stats", sp_stats)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/chat", sp_chat)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/faq", sp_faq_add)
    app.router.add_patch("/api/miniapp/sales/faq/{fid}", sp_faq_update)
    app.router.add_delete("/api/miniapp/sales/faq/{fid}", sp_faq_delete)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/example", sp_example_add)
    app.router.add_delete("/api/miniapp/sales/example/{eid}", sp_example_delete)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/delivery", sp_delivery_add)
    app.router.add_delete("/api/miniapp/sales/delivery/{did}", sp_delivery_delete)
    app.router.add_post("/api/miniapp/sales/persona/{pid}/promo", sp_promo_add)
    app.router.add_delete("/api/miniapp/sales/promo/{prid}", sp_promo_delete)
    app.router.add_get("/api/miniapp/sales/orders", sp_orders)
    app.router.add_patch("/api/miniapp/sales/order/{oid}", sp_order_update)
