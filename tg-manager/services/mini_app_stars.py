"""Звёзды, подарки и продвижение — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 17 маршрутов трёх блоков, которые
владелец видит как одну работу «вложить звёзды и получить рост».

* `/api/miniapp/stars*` — сводка по звёздам и A/B-эксперименты над ценой:
  создание, карточка, включение, оценка победителя, удаление.
* `/api/miniapp/gifts*` — инвентарь подарков, сканирование, предпросмотр и
  отправка передачи, список получателей и добавление получателя.
* `/api/miniapp/promo*` — сводка продвижения, заказ, отмена и ускорение
  заказа, добавление бота-склада.

Вместе с обработчиками уехали четыре вещи, которых больше никто не зовёт:
`_GIFT_PAY` (допустимые способы оплаты), `_gift_recipient` (разбор
получателя с русской причиной отказа), `_gift_selection` (выбор подарков
владельца — одна дверь для предпросмотра и для отправки, чтобы отправка не
брала то, чего не показал предпросмотр) и `_own_stars_exp` (эксперимент
принадлежит владельцу).

`MIN_IMPRESSIONS_FOR_WINNER` импортируется из `services.stars_optimizer` под
тем же именем `_STARS_MIN_IMPRESSIONS`: порог показов, ниже которого
победителя объявлять нельзя, считает оптимизатор, а не экран. Второго
источника правды здесь не появляется — это держит
`tests/test_stars_experiment_is_readable.py`.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import re

from aiohttp import web

from services import operation_bus as _obus
from services.stars_optimizer import MIN_IMPRESSIONS_FOR_WINNER as _STARS_MIN_IMPRESSIONS


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты звёзд, подарков и продвижения."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _safe_fetch,
        _safe_fetchrow, log,
    )
    from services.security import validate_string

    _GIFT_PAY = ("stars", "wallet", "auto")

    async def gift_inventory(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            table_exists = await pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='gift_inventory')"
            )
            if not table_exists:
                return _json_resp({"total": 0, "transferable": 0, "items": [], "note": "Таблица подарков ещё не создана. Запустите сканирование через бота."})
            items = await pool.fetch(
                # Колонка называется last_seen_at; scanned_at — имя поля в
                # ответе, поэтому переименование остаётся псевдонимом и фронт
                # не трогаем.
                """SELECT gi.id, gi.gift_type, gi.stars_cost, gi.is_transferable,
                          gi.is_unique, gi.is_premium, gi.last_seen_at AS scanned_at,
                          gi.account_id, gi.gift_id,
                          ta.phone, ta.first_name
                   FROM gift_inventory gi
                   JOIN tg_accounts ta ON ta.id=gi.account_id
                   WHERE gi.owner_id=$1
                   ORDER BY gi.last_seen_at DESC LIMIT 100""",
                uid,
            )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM gift_inventory WHERE owner_id=$1", uid
            )
            transferable = await pool.fetchval(
                "SELECT COUNT(*) FROM gift_inventory WHERE owner_id=$1 AND is_transferable=TRUE", uid
            )
            return _json_resp({
                "total": total, "transferable": transferable,
                "items": [
                    {**dict(i), "scanned_at": i["scanned_at"].isoformat() if i.get("scanned_at") else None}
                    for i in items
                ],
            })
        except Exception:
            log.exception("gift_inventory uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def gift_scan_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            label = "Сканирование подарков во всех аккаунтах"
            op_id = await _obus.submit(
                pool, uid, "gift_scan", {}, total_items=1, label=label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) — это НЕ сбой:
            # честный 403 с причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("gift_scan_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    def _gift_recipient(body: dict) -> tuple[str, str]:
        """@username получателя из тела запроса. Возвращает (username, ошибка).

        Имя приходит от пользователя и уходит в текст подтверждения и в план,
        поэтому режем по формату Telegram, а не доверяем строке.
        """
        raw = (body.get("recipient") or "").strip()
        uname = raw.lstrip("@").split("/")[-1].split("?")[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", uname or ""):
            return "", "Укажите @username получателя (4–32 символа, латиница)"
        return uname, ""

    async def _gift_selection(uid: int, body: dict) -> tuple[list[dict], str]:
        """Выбранные подарки ВЛАДЕЛЬЦА. Возвращает (позиции, ошибка)."""
        ids = [int(x) for x in (body.get("inventory_ids") or [])
               if str(x).lstrip("-").isdigit()][:200]
        if not ids:
            return [], "Выберите хотя бы один подарок"
        rows = await _safe_fetch(pool,
            "SELECT id, account_id, gift_id, gift_type, stars_cost, is_transferable "
            "FROM gift_inventory WHERE owner_id=$1 AND id = ANY($2::bigint[])",
            uid, ids)
        mine = [dict(r) for r in (rows or [])]
        if not mine:
            return [], "Подарки не найдены"
        # Непередаваемые отсекаем здесь, а не в движке: иначе владелец увидит в
        # предпросмотре стоимость за то, что всё равно будет пропущено.
        can = [g for g in mine if g.get("is_transferable")]
        if not can:
            return [], "Ни один из выбранных подарков нельзя передать"
        return can, ""

    async def gift_transfer_preview(request: web.Request) -> web.Response:
        """Что именно уйдёт и во сколько обойдётся — ДО нажатия.

        Передача необратима, поэтому предпросмотр ничего не создаёт: план
        появляется только при самой отправке, иначе брошенные предпросмотры
        копились бы в базе планами-призраками.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        try:
            gifts, err = await _gift_selection(uid, body)
            if err:
                return _err(err, 400)
            asked = len([x for x in (body.get("inventory_ids") or [])])
            stars = sum(int(g.get("stars_cost") or 0) for g in gifts)
            uname, uerr = _gift_recipient(body)
            return _json_resp({
                "count": len(gifts),
                "skipped": max(0, asked - len(gifts)),
                "stars": stars,
                "recipient": uname,
                "recipient_error": uerr,
                "payment_source": (body.get("payment_source") or "auto"),
            })
        except Exception:
            log.exception("gift_transfer_preview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def gift_transfer_submit(request: web.Request) -> web.Response:
        """Поставить передачу подарков в очередь операций."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        try:
            uname, uerr = _gift_recipient(body)
            if uerr:
                return _err(uerr, 400)
            gifts, err = await _gift_selection(uid, body)
            if err:
                return _err(err, 400)
            pay = (body.get("payment_source") or "auto").strip().lower()
            if pay not in _GIFT_PAY:
                pay = "auto"
            from services.gift_transfer import GiftTransferService as _gts
            plan_id = await _gts.create_plan(
                pool, uid, uname, 0, f"@{uname}", pay)
            await _gts.add_items_to_plan(pool, plan_id, [
                {"inventory_id": g["id"], "account_id": g["account_id"],
                 "gift_id": g["gift_id"], "gift_type": g["gift_type"],
                 "stars_cost": int(g.get("stars_cost") or 0)}
                for g in gifts])
            check = await _gts.validate_plan(pool, plan_id)
            if not check.get("valid"):
                return _err("; ".join(check.get("errors") or []) or
                            "План передачи не прошёл проверку", 400)
            label = f"Передача подарков @{uname}: {len(gifts)}"
            op_id = await _obus.submit(
                pool, uid, "gift_transfer", {"plan_id": plan_id},
                total_items=len(gifts), label=label)
            await pool.execute(
                "UPDATE gift_transfer_plans SET status='queued' WHERE id=$1 AND owner_id=$2",
                plan_id, uid)
            return _json_resp({"ok": True, "op_id": op_id, "plan_id": plan_id,
                               "count": len(gifts), "label": label,
                               "warnings": check.get("warnings") or []})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("gift_transfer_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def gift_recipients(request: web.Request) -> web.Response:
        """Сохранённые получатели владельца: в базе и в боте были, в приложении нет."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await _safe_fetch(pool,
                "SELECT id, name, username, is_main_admin FROM gift_recipients "
                "WHERE owner_id=$1 ORDER BY is_main_admin DESC, name LIMIT 100",
                uid)
            return _json_resp({"recipients": [dict(r) for r in (rows or [])]})
        except Exception:
            log.exception("gift_recipients uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def gift_recipient_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        try:
            uname, uerr = _gift_recipient(body)
            if uerr:
                return _err(uerr, 400)
            name = validate_string(body.get("name") or f"@{uname}", 64, False) or f"@{uname}"
            rid = await pool.fetchval(
                "INSERT INTO gift_recipients (owner_id, name, username) "
                "VALUES ($1,$2,$3) ON CONFLICT (owner_id, name) DO UPDATE "
                "SET username=EXCLUDED.username, updated_at=now() RETURNING id",
                uid, name, uname)
            return _json_resp({"ok": True, "id": rid, "name": name, "username": uname})
        except Exception:
            log.exception("gift_recipient_add uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def stars_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            summary = await pool.fetchrow(
                """SELECT
                   COALESCE(SUM(revenue_a + revenue_b), 0) AS total_revenue,
                   COALESCE(SUM(conversions_a + conversions_b), 0) AS total_conversions,
                   COUNT(*) FILTER (WHERE status='active') AS active_exps,
                   COUNT(*) AS total_exps
                   FROM stars_experiments WHERE owner_id=$1""",
                uid,
            )
            exps = await pool.fetch(
                """SELECT id, name, status, winner, price_a, price_b,
                          impressions_a, conversions_a, revenue_a,
                          impressions_b, conversions_b, revenue_b,
                          created_at
                   FROM stars_experiments WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 20""",
                uid,
            )
            return _json_resp({
                "total_revenue": summary["total_revenue"],
                "total_conversions": summary["total_conversions"],
                "active_exps": summary["active_exps"],
                "total_exps": summary["total_exps"],
                "experiments": [
                    {**dict(e), "created_at": e["created_at"].isoformat() if e["created_at"] else None}
                    for e in exps
                ],
            })
        except Exception:
            log.exception("stars_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _own_stars_exp(uid: int, exp_id: int):
        return await _safe_fetchrow(pool,
            "SELECT * FROM stars_experiments WHERE id=$1 AND owner_id=$2", exp_id, uid)

    async def stars_experiment_detail(request: web.Request) -> web.Response:
        """Эксперимент по цене целиком: выручка по вариантам и достоверность.

        Оценку (хи-квадрат, significance) движок считает раз в шесть часов и
        показывал только в Telegram-боте; в мини-аппе строка эксперимента
        давала лишь «A:⭐50 CR12% · B:⭐100 CR8%» и кнопку паузы. Здесь тот же
        расчёт, но ТОЛЬКО на чтение: сохраняет вывод отдельная кнопка.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор теста", 400)
        row = await _own_stars_exp(uid, exp_id)
        if not row:
            return _err("Тест не найден", 404)
        try:
            from services import stars_optimizer as _so
            imp_a, imp_b = int(row["impressions_a"] or 0), int(row["impressions_b"] or 0)
            conv_a, conv_b = int(row["conversions_a"] or 0), int(row["conversions_b"] or 0)
            p_val = _so._chi_square_p(conv_a, imp_a, conv_b, imp_b)
            enough = imp_a >= _STARS_MIN_IMPRESSIONS and imp_b >= _STARS_MIN_IMPRESSIONS
            exp = dict(row)
            for key in ("created_at", "completed_at"):
                if exp.get(key):
                    exp[key] = exp[key].isoformat()
            recs = []
            try:
                recs = await _so.get_recommendations(pool, int(row["bot_id"]), uid)
            except Exception:
                log.debug("stars recommendations failed exp=%s", exp_id)
            return _json_resp({
                "experiment": exp,
                "verdict": {
                    "cr_a": (conv_a / imp_a) if imp_a else 0.0,
                    "cr_b": (conv_b / imp_b) if imp_b else 0.0,
                    "p_value": p_val,
                    "enough_data": enough,
                    "confident": bool(enough and p_val < 0.05),
                    "min_impressions": _STARS_MIN_IMPRESSIONS,
                    "need_a": max(0, _STARS_MIN_IMPRESSIONS - imp_a),
                    "need_b": max(0, _STARS_MIN_IMPRESSIONS - imp_b),
                },
                "recommendations": recs,
            })
        except Exception:
            log.exception("stars_experiment_detail uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def stars_experiment_evaluate(request: web.Request) -> web.Response:
        """Подвести итог: та же оценка, что у фонового движка, но по кнопке."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор теста", 400)
        if not await _own_stars_exp(uid, exp_id):
            return _err("Тест не найден", 404)
        try:
            from services import stars_optimizer as _so
            res = await _so.evaluate_experiment(pool, exp_id)
            return _json_resp(res)
        except Exception:
            log.exception("stars_experiment_evaluate uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def stars_experiment_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор теста", 400)
        try:
            gone = await _safe_fetchrow(pool,
                "DELETE FROM stars_experiments WHERE id=$1 AND owner_id=$2 RETURNING id",
                exp_id, uid)
            if not gone:
                return _err("Тест не найден", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("stars_experiment_delete uid=%d exp=%d", uid, exp_id)
            return _err(_INTERNAL_ERROR, 500)

    async def stars_experiment_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        bot_id = data.get("bot_id")
        name = str(data.get("name", "")).strip()
        content_type = data.get("content_type", "message")
        price_a = int(data.get("price_a", 50))
        price_b = int(data.get("price_b", 100))
        if not bot_id or not name:
            return _err("bot_id и name обязательны", 400)
        if content_type not in ("message", "media", "subscription", "gift"):
            content_type = "message"
        if price_a < 1 or price_b < 1:
            return _err("Цены должны быть > 0", 400)
        try:
            bot = await pool.fetchrow(
                "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", int(bot_id), uid
            )
            if not bot:
                return _err("Бот не найден", 404)
            eid = await pool.fetchval(
                """INSERT INTO stars_experiments
                   (bot_id, owner_id, name, content_type, price_a, price_b, status)
                   VALUES ($1, $2, $3, $4, $5, $6, 'active') RETURNING id""",
                int(bot_id), uid, name, content_type, price_a, price_b,
            )
            return _json_resp({"id": eid, "ok": True})
        except Exception:
            log.exception("stars_experiment_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def stars_experiment_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eid = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор эксперимента", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, status FROM stars_experiments WHERE id=$1 AND owner_id=$2", eid, uid
            )
            if not row:
                return _err("Не найдено", 404)
            new_status = "paused" if row["status"] == "active" else "active"
            await pool.execute(
                "UPDATE stars_experiments SET status=$1 WHERE id=$2 AND owner_id=$3", new_status, eid, uid
            )
            return _json_resp({"status": new_status})
        except Exception:
            log.exception("stars_experiment_toggle uid=%d exp=%d", uid, eid)
            return _err(_INTERNAL_ERROR, 500)

    async def promo_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            orders = await pool.fetch(
                "SELECT id, keyword, status, target_position, current_subs, target_subs, "
                "bot_id, smm_panel_id, created_at "
                "FROM promo_orders WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50",
                uid,
            )
            bots = await pool.fetch(
                "SELECT id, bot_username, status, current_subs, ready_at, created_at "
                "FROM bot_warehouse WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50",
                uid,
            )
            panels = await pool.fetch(
                "SELECT id, name, api_url, is_active FROM smm_panels WHERE owner_id=$1 ORDER BY created_at DESC",
                uid,
            )
        except Exception:
            log.exception("promo_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({
            "orders": [
                {
                    "id": r["id"], "keyword": r["keyword"] or "",
                    "status": r["status"] or "waiting",
                    "target_position": r["target_position"],
                    "current_subs": r["current_subs"],
                    "target_subs": r["target_subs"],
                    "bot_id": r["bot_id"],
                    "smm_panel_id": r["smm_panel_id"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in orders
            ],
            "bots": [
                {
                    "id": r["id"], "bot_username": r["bot_username"] or "",
                    "status": r["status"] or "aging",
                    "current_subs": r["current_subs"] or 0,
                    "ready_at": r["ready_at"].isoformat() if r["ready_at"] else None,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in bots
            ],
            "panels": [
                {"id": r["id"], "name": r["name"] or "", "api_url": r["api_url"] or "", "is_active": r["is_active"]}
                for r in panels
            ],
        })

    async def promo_cancel_order(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            order_id = int(request.match_info["order_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор заказа", 400)
        try:
            res = await pool.execute(
                "UPDATE promo_orders SET status='cancelled', updated_at=NOW() WHERE id=$1 AND owner_id=$2",
                order_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Не найдено", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("promo_cancel_order uid=%d order=%d", uid, order_id)
            return _err(_INTERNAL_ERROR, 500)

    async def promo_create_order_api(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            keyword = str(body.get("keyword", "")).strip()
            target_position = int(body.get("target_position", 1))
            target_subs = body.get("target_subs")
            bot_id = body.get("bot_id")
            smm_panel_id = body.get("smm_panel_id")
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not keyword:
            return _err("keyword обязателен", 400)
        if target_position < 1 or target_position > 50:
            return _err("Позиция цели — от 1 до 50", 400)

        # Бот и панель приходят из тела запроса, то есть от клиента. Без этой
        # проверки заказ можно было создать с ЧУЖИМ smm_panel_id: запуск
        # накрутки уходил в чужую панель с чужим API-ключом и тратил чужие
        # деньги. Проверяем принадлежность здесь, при записи, чтобы в заказе
        # заведомо не могло оказаться чужого номера.
        from database import db as _db

        bot_id_val = int(bot_id) if bot_id else None
        panel_id_val = int(smm_panel_id) if smm_panel_id else None
        if bot_id_val is not None and not await _db.warehouse_get_bot(
            pool, bot_id_val, owner_id=uid
        ):
            return _err("Бот не найден в вашем складе", 404)
        if panel_id_val is not None and not await _db.smm_get_panel(
            pool, panel_id_val, owner_id=uid
        ):
            return _err("SMM-панель не найдена", 404)

        try:
            order_id = await pool.fetchval(
                """INSERT INTO promo_orders(owner_id, keyword, target_position, bot_id, smm_panel_id, target_subs)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                uid, keyword, target_position,
                bot_id_val,
                panel_id_val,
                int(target_subs) if target_subs else None,
            )
        except Exception:
            log.exception("promo_create_order uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "order_id": order_id})

    async def promo_order_boost(request: web.Request) -> web.Response:
        """Отправить заказ в SMM-панель — то же самое, что кнопка «🚀 Накрутить»
        в боте (promo_platform.py:cb_order_boost). Раньше в mini_app не было
        способа привязать заказ к боту/панели или запустить накрутку — заказ,
        созданный тут, навсегда оставался в статусе 'waiting'."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            order_id = int(request.match_info["order_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор заказа", 400)

        from database import db
        from services import smm_panel as smm_svc

        order = await db.promo_get_order(pool, order_id, owner_id=uid)
        if not order:
            return _err("Заказ не найден", 404)
        if not order["bot_id"] or not order["smm_panel_id"]:
            return _err("У заказа не выбраны бот и SMM-панель", 400)

        # Номера бота и панели лежат в строке заказа, но попали туда из тела
        # запроса — второй рубеж на случай заказов, созданных до проверки при
        # записи, и на случай бота/панели, удалённых или сменивших владельца.
        panel = await db.smm_get_panel(pool, order["smm_panel_id"], owner_id=uid)
        if not panel:
            return _err("SMM-панель не найдена", 404)
        bot_rec = await db.warehouse_get_bot(pool, order["bot_id"], owner_id=uid)
        if not bot_rec:
            return _err("Бот не найден в складе", 404)

        link = f"https://t.me/{bot_rec['bot_username']}"
        client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
        result = await client.add_order(
            service_id=panel["service_id"] or "",
            link=link,
            quantity=int(order["target_subs"] or 100),
        )
        if result.get("error") or "order" not in result:
            err_msg = result.get("error", str(result)[:200])
            await db.promo_log(
                pool, uid, "booster",
                f"Ошибка запуска накрутки заказ #{order['id']}: {err_msg}",
                level="ERROR", order_id=order["id"],
                meta={"panel": panel["name"], "link": link},
            )
            return _err(f"Ошибка панели: {str(err_msg)[:300]}", 400)

        smm_order_id = str(result["order"])
        await db.promo_update_order_status(
            pool, order["id"], "boosting",
            smm_order_id=smm_order_id,
            smm_panel_id=panel["id"],
        )
        await db.warehouse_update_bot(pool, bot_rec["id"], uid, status="working")
        await db.promo_log(
            pool, uid, "booster",
            f"Накрутка запущена: заказ #{order['id']} → панель #{smm_order_id}",
            order_id=order["id"],
            meta={"panel": panel["name"], "smm_order_id": smm_order_id, "link": link},
        )
        return _json_resp({"ok": True, "smm_order_id": smm_order_id})

    async def promo_add_warehouse_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            bot_username = str(body.get("bot_username", "")).strip().lstrip("@")
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not bot_username:
            return _err("bot_username обязателен", 400)
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(tz=timezone.utc)
            ready_at = now + timedelta(days=21)
            bot_id = await pool.fetchval(
                """INSERT INTO bot_warehouse(owner_id, bot_username, status, registered_at, ready_at)
                   VALUES($1,$2,'aging',$3,$4) RETURNING id""",
                uid, bot_username, now, ready_at,
            )
        except Exception:
            log.exception("promo_add_warehouse_bot uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "bot_id": bot_id, "ready_at": ready_at.isoformat()})

    app.router.add_get("/api/miniapp/promo", promo_overview)
    app.router.add_post("/api/miniapp/promo/order", promo_create_order_api)
    app.router.add_post("/api/miniapp/promo/order/{order_id}/cancel", promo_cancel_order)
    app.router.add_post("/api/miniapp/promo/order/{order_id}/boost", promo_order_boost)
    app.router.add_post("/api/miniapp/promo/warehouse/bot", promo_add_warehouse_bot)
    app.router.add_get("/api/miniapp/gifts", gift_inventory)
    app.router.add_post("/api/miniapp/gifts/scan", gift_scan_submit)
    app.router.add_post("/api/miniapp/gifts/transfer/preview", gift_transfer_preview)
    app.router.add_post("/api/miniapp/gifts/transfer", gift_transfer_submit)
    app.router.add_get("/api/miniapp/gifts/recipients", gift_recipients)
    app.router.add_post("/api/miniapp/gifts/recipients", gift_recipient_add)
    app.router.add_get("/api/miniapp/stars", stars_overview)
    app.router.add_post("/api/miniapp/stars/experiment", stars_experiment_create)
    app.router.add_put("/api/miniapp/stars/experiment/{exp_id}/toggle", stars_experiment_toggle)
    app.router.add_get("/api/miniapp/stars/experiment/{exp_id}", stars_experiment_detail)
    app.router.add_post("/api/miniapp/stars/experiment/{exp_id}/evaluate", stars_experiment_evaluate)
    app.router.add_delete("/api/miniapp/stars/experiment/{exp_id}", stars_experiment_delete)
