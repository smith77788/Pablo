"""Воронки и авто-воронки — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 14 маршрутов двух родственных блоков.
`/api/miniapp/funnel*` — ручная воронка и её шаги (список, добавление,
правка, удаление, переключение). `/api/miniapp/auto_funnel*` — авто-воронка:
список, создание, подробности с движением людей по шагам, шаги и удаление.

Блоки лежали в монолите в двух разных местах, но делят обе проверки доступа,
поэтому и вынесены вместе:

* `_own_funnel(uid, fid)` — воронка принадлежит владельцу (`WHERE id=$1 AND
  owner_id=$2`). Её держит `tests/test_miniapp_path_param_scope_ratchet.py`:
  без неё по `{funnel_id}` из запроса можно было бы править чужую воронку.
* `_funnel_step_fields(body)` — разбор полей шага с РУССКОЙ причиной отказа;
  одна дверь для добавления и для правки, чтобы правка не принимала то, чего
  не принимает добавление.

Оба замыкания использовались только этими обработчиками, поэтому уехали
целиком, а не импортируются.

Удаление шага перенумеровывает оставшиеся подряд и сдвигает активные
прохождения: дырка в нумерации оставила бы людей на шаге, которого больше
нет, и цепочка встала бы молча (`tests/test_funnel_steps_editable_in_app.py`).

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты воронок и авто-воронок."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _safe_count,
        _safe_fetch, _safe_fetchrow, log,
    )
    from services.security import (
        check_sql_suspicious, validate_integer, validate_string,
    )


    async def toggle_funnel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE funnels SET is_active = NOT is_active
                   WHERE id=$1 AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)
                   RETURNING id, is_active""",
                funnel_id, uid)
            if not row:
                return _err("Не найдено", 404)
            return _json_resp({"ok": True, "is_active": row["is_active"]})
        except Exception:
            log.exception("toggle_funnel")
            return _err("Не удалось переключить", 500)

    async def funnels_all(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT f.id, f.name, f.is_active, mb.username AS bot_username,
                          COUNT(fs.id) FILTER (WHERE fs.completed IS NOT TRUE) AS active_subs,
                          COUNT(fs.id) FILTER (WHERE fs.completed IS TRUE) AS completed_subs
                   FROM funnels f
                   JOIN managed_bots mb ON mb.bot_id=f.bot_id
                   -- Псевдонима fs не было вовсе: запрос ссылался на таблицу,
                   -- которую не подключал, падал всегда, и экран «Воронки»
                   -- отдавал пустой список при любом числе воронок.
                   LEFT JOIN funnel_subscriptions fs ON fs.funnel_id=f.id
                   WHERE mb.added_by=$1
                   GROUP BY f.id, f.name, f.is_active, mb.username
                   ORDER BY active_subs DESC LIMIT 30""", uid)
            return _json_resp({"funnels": [dict(r) for r in rows]})
        except Exception as e:
            log.warning("funnels_all uid=%d: %s", uid, e)
            return _json_resp({"funnels": []})

    async def funnel_steps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        # Verify ownership via managed_bots
        funnel = await _safe_fetchrow(pool,
            """SELECT f.id, f.name, f.trigger_type, f.keyword, f.is_active, mb.username AS bot_username
               FROM funnels f JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE f.id=$1 AND mb.added_by=$2""", funnel_id, uid)
        if not funnel:
            return _err("Воронка не найдена", 404)
        steps = await _safe_fetch(pool,
            "SELECT id, step_order, message_text, delay_minutes FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order",
            funnel_id)
        subs_active = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnel_subscriptions WHERE funnel_id=$1 AND completed=false", funnel_id)
        subs_total = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnel_subscriptions WHERE funnel_id=$1", funnel_id)

        # Отвал по шагам. funnel_subscriptions.current_step (сколько шагов уже
        # доставлено) копится с самого начала, но нигде не агрегировался: было
        # видно только «всего/активных», а на каком шаге люди уходят — главный
        # вопрос к любой цепочке — узнать было негде.
        _dist = await _safe_fetch(
            pool,
            """SELECT current_step,
                      COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE dropped)   AS dropped_n,
                      COUNT(*) FILTER (WHERE completed) AS completed_n
                 FROM funnel_subscriptions
                WHERE funnel_id=$1
             GROUP BY current_step""",
            funnel_id,
        )
        _by_step = {int(r["current_step"] or 0): r for r in _dist}
        _delivered_at_least = {}   # сколько подписчиков получили >= i шагов
        _max_step = max(_by_step) if _by_step else 0
        _running = 0
        for _i in range(_max_step, -1, -1):
            _running += int((_by_step.get(_i) or {}).get("n") or 0)
            _delivered_at_least[_i] = _running
        step_stats = []
        for _idx, _s in enumerate(steps, start=1):
            _reached = _delivered_at_least.get(_idx, 0)
            _next = _delivered_at_least.get(_idx + 1, 0)
            step_stats.append({
                "step_order": _s.get("step_order", _idx),
                "delivered": _reached,
                # Ушли, не получив следующий шаг: здесь и виден отвал.
                "lost_after": max(0, _reached - _next) if _idx < len(steps) else 0,
                "conversion_pct": (round(_reached * 100.0 / subs_total, 1)
                                   if subs_total else 0.0),
            })
        dropped_total = sum(int(r["dropped_n"] or 0) for r in _dist)
        completed_total = sum(int(r["completed_n"] or 0) for r in _dist)

        return _json_resp({
            "funnel": funnel,
            "steps": steps,
            "subs_active": subs_active,
            "subs_total": subs_total,
            "step_stats": step_stats,
            "dropped_total": dropped_total,
            "completed_total": completed_total,
        })

    async def add_funnel_step(request: web.Request) -> web.Response:
        """Добавить шаг в воронку (multi-step drip). funnel_steps умел delay_minutes и
        порядок, но эндпоинта добавления шага не было — воронка застревала на шаге 1."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        # Владение через managed_bots.
        funnel = await _safe_fetchrow(pool,
            """SELECT f.id FROM funnels f JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE f.id=$1 AND mb.added_by=$2""", funnel_id, uid)
        if not funnel:
            return _err("Воронка не найдена", 404)
        message_text = (body.get("message_text") or "").strip()
        if not message_text:
            return _err("Текст шага обязателен", 400)
        if len(message_text) > 4096:
            return _err("Текст шага — до 4096 символов", 400)
        try:
            delay_minutes = max(0, min(43200, int(body.get("delay_minutes") or 0)))  # ≤30 дней
        except (TypeError, ValueError):
            return _err("delay_minutes должен быть числом", 400)
        # Ограничение на число шагов (защита от разрастания).
        cnt = await _safe_count(pool, "SELECT COUNT(*) FROM funnel_steps WHERE funnel_id=$1", funnel_id)
        if cnt >= 50:
            return _err("Достигнут лимит шагов (50)", 400)
        try:
            row = await pool.fetchrow(
                """INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes)
                   VALUES($1, COALESCE((SELECT MAX(step_order) FROM funnel_steps WHERE funnel_id=$1),0)+1, $2, $3)
                   RETURNING id, step_order""",
                funnel_id, message_text, delay_minutes)
            return _json_resp({"ok": True, "step_id": row["id"], "step_order": row["step_order"]})
        except Exception:
            log.exception("add_funnel_step funnel=%d uid=%d", funnel_id, uid)
            return _err("Не удалось добавить шаг", 500)

    async def delete_funnel_step(request: web.Request) -> web.Response:
        """Удалить шаг воронки. Шаг 1 (первое сообщение) удалить нельзя — это триггерный старт."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            step_id = int(request.match_info["step_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шага", 400)
        # Владение + защита шага 1 через JOIN.
        step = await _safe_fetchrow(pool,
            """SELECT fs.id, fs.step_order FROM funnel_steps fs
               JOIN funnels f ON f.id=fs.funnel_id
               JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE fs.id=$1 AND mb.added_by=$2""", step_id, uid)
        if not step:
            return _err("Шаг не найден", 404)
        if int(step["step_order"]) <= 1:
            return _err("Первый шаг удалить нельзя (это стартовое сообщение)", 400)
        try:
            await pool.execute("DELETE FROM funnel_steps WHERE id=$1", step_id)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_funnel_step step=%d uid=%d", step_id, uid)
            return _err("Не удалось удалить шаг", 500)

    async def update_funnel_step(request: web.Request) -> web.Response:
        """Изменить текст и задержку существующего шага воронки.

        Правки шага в мини-приложении не было вовсе — только добавить и удалить.
        Опечатка в третьем шаге из пяти означала удалить его и добавить заново,
        а добавление кладёт шаг В КОНЕЦ: пользователь чинил опечатку ценой
        поломанного порядка цепочки. В боте правка при этом давно есть.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            step_id = int(request.match_info["step_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шага", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        # Владение — тем же JOIN, что у удаления.
        step = await _safe_fetchrow(pool,
            """SELECT fs.id FROM funnel_steps fs
               JOIN funnels f ON f.id=fs.funnel_id
               JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE fs.id=$1 AND mb.added_by=$2""", step_id, uid)
        if not step:
            return _err("Шаг не найден", 404)

        sets: list[str] = []
        args: list = []
        if body.get("message_text") is not None:
            text = validate_string(body.get("message_text"), max_len=4096)
            if not text:
                return _err("Текст шага не может быть пустым", 400)
            if check_sql_suspicious(text):
                return _err("Недопустимые символы в тексте", 400)
            args.append(text)
            sets.append(f"message_text=${len(args)}")
        if body.get("delay_minutes") is not None:
            _d = validate_integer(body.get("delay_minutes"), min_val=0, max_val=100000)
            if _d is None:
                return _err("Задержка должна быть числом", 400)
            args.append(int(_d))
            sets.append(f"delay_minutes=${len(args)}")
        if not sets:
            return _err("Нечего изменять", 400)
        args.append(step_id)
        try:
            await pool.execute(
                f"UPDATE funnel_steps SET {', '.join(sets)} WHERE id=${len(args)}", *args)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("update_funnel_step step=%d uid=%d", step_id, uid)
            return _err("Не удалось изменить шаг", 500)

    async def auto_funnels_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT f.id, f.name, f.bot_id, mb.username AS bot_username,
                       mb.first_name AS bot_name, f.target_segment, f.enabled, f.created_at,
                       COUNT(DISTINCT fs.id) AS steps_count,
                       COUNT(DISTINCT fr.id) FILTER (WHERE fr.status='active') AS active_runs
                FROM auto_funnels f
                LEFT JOIN managed_bots mb ON mb.bot_id = f.bot_id
                LEFT JOIN auto_funnel_steps fs ON fs.funnel_id = f.id
                LEFT JOIN auto_funnel_runs fr ON fr.funnel_id = f.id
                WHERE f.owner_id = $1
                GROUP BY f.id, f.name, f.bot_id, mb.username, mb.first_name,
                         f.target_segment, f.enabled, f.created_at
                ORDER BY f.id DESC
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("auto_funnels_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def auto_funnel_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        try:
            funnel = await pool.fetchrow(
                "SELECT enabled FROM auto_funnels WHERE id=$1 AND owner_id=$2", fid, uid
            )
        except Exception:
            log.exception("auto_funnel_toggle fetch uid=%d fid=%d", uid, fid)
            return _err(_INTERNAL_ERROR, 500)
        if not funnel:
            return _err("Не найдено", 404)
        new_state = not funnel["enabled"]
        try:
            await pool.execute(
                "UPDATE auto_funnels SET enabled=$1, updated_at=NOW() WHERE id=$2 AND owner_id=$3", new_state, fid, uid
            )
        except Exception:
            log.exception("auto_funnel_toggle update uid=%d fid=%d", uid, fid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"enabled": new_state})

    async def auto_funnel_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        try:
            funnel = await pool.fetchrow(
                """
                SELECT f.*, mb.username AS bot_username, mb.first_name AS bot_name
                FROM auto_funnels f
                LEFT JOIN managed_bots mb ON mb.bot_id = f.bot_id
                WHERE f.id=$1 AND f.owner_id=$2
                """,
                fid, uid,
            )
            if not funnel:
                return _err("Не найдено", 404)
            steps = await pool.fetch(
                "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num", fid
            )
            # Сколько людей в воронке и где они стоят. Экран показывал одно
            # число «Активных: N» в списке и ничего — в самой воронке: нельзя
            # было понять, на каком шаге люди застревают.
            runs = await _safe_fetch(pool,
                """SELECT status, next_step_num, COUNT(*) AS cnt
                     FROM auto_funnel_runs WHERE funnel_id=$1
                 GROUP BY status, next_step_num""", fid)
            by_status: dict = {}
            by_step: dict = {}
            for r in (runs or []):
                by_status[r["status"]] = by_status.get(r["status"], 0) + int(r["cnt"])
                if r["status"] == "active":
                    key = str(r["next_step_num"])
                    by_step[key] = by_step.get(key, 0) + int(r["cnt"])
            return _json_resp({
                "funnel": dict(funnel), "steps": [dict(s) for s in steps],
                "runs": {"by_status": by_status, "waiting_on_step": by_step,
                         "total": sum(by_status.values())},
            })
        except Exception:
            log.exception("auto_funnel_detail uid=%d fid=%d", uid, fid)
            return _err(_INTERNAL_ERROR, 500)

    async def _own_funnel(uid: int, fid: int):
        return await _safe_fetchrow(pool,
            "SELECT id FROM auto_funnels WHERE id=$1 AND owner_id=$2", fid, uid)

    def _funnel_step_fields(body: dict) -> tuple[dict | None, str]:
        """Разобрать шаг воронки. Текст обязателен, кнопка — только с ссылкой."""
        text = validate_string(body.get("message_text"), max_len=4000)
        if not text:
            return None, "Текст сообщения обязателен"
        delay = validate_integer(body.get("delay_hours", 0), min_val=0, max_val=24 * 365)
        if delay is None:
            return None, "Задержка — целое число часов от 0 до 8760"
        btn_text = validate_string(body.get("button_text") or "", max_len=64, required=False) or None
        btn_url = validate_string(body.get("button_url") or "", max_len=2048, required=False) or None
        if btn_url and not btn_url.lower().startswith(("http://", "https://", "tg://")):
            return None, "Ссылка кнопки должна начинаться с http://, https:// или tg://"
        if btn_text and not btn_url:
            return None, "У кнопки нет ссылки — её некуда нажимать"
        if btn_url and not btn_text:
            return None, "У кнопки нет подписи"
        return {"message_text": text, "delay_hours": delay,
                "button_text": btn_text, "button_url": btn_url}, ""

    async def auto_funnel_step_add(request: web.Request) -> web.Response:
        """Добавить шаг. Раньше шаги заводились только из Telegram-бота."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        fields, err = _funnel_step_fields(body)
        if err:
            return _err(err, 400)
        if not await _own_funnel(uid, fid):
            return _err("Воронка не найдена", 404)
        try:
            # Номер шага считаем в той же транзакции, что и вставку: иначе два
            # одновременных добавления получат одинаковый step_num и упрутся в
            # UNIQUE (funnel_id, step_num).
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Блокируем саму воронку: FOR UPDATE с агрегатом Postgres
                    # не разрешает, а без блокировки два одновременных
                    # добавления возьмут один и тот же номер.
                    await conn.fetchval(
                        "SELECT id FROM auto_funnels WHERE id=$1 FOR UPDATE", fid)
                    nxt = await conn.fetchval(
                        "SELECT COALESCE(MAX(step_num), 0) + 1 FROM auto_funnel_steps "
                        "WHERE funnel_id=$1", fid)
                    sid = await conn.fetchval(
                        """INSERT INTO auto_funnel_steps
                           (funnel_id, step_num, delay_hours, message_text,
                            button_text, button_url)
                           VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                        fid, int(nxt or 1), fields["delay_hours"], fields["message_text"],
                        fields["button_text"], fields["button_url"])
            return _json_resp({"ok": True, "id": sid, "step_num": int(nxt or 1)})
        except Exception:
            log.exception("auto_funnel_step_add uid=%d fid=%d", uid, fid)
            return _err(_INTERNAL_ERROR, 500)

    async def auto_funnel_step_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
            sid = int(request.match_info["step_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шага", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        fields, err = _funnel_step_fields(body)
        if err:
            return _err(err, 400)
        if not await _own_funnel(uid, fid):
            return _err("Воронка не найдена", 404)
        try:
            # Шаг обязан принадлежать именно этой воронке.
            upd = await _safe_fetchrow(pool,
                """UPDATE auto_funnel_steps
                      SET delay_hours=$3, message_text=$4, button_text=$5, button_url=$6
                    WHERE id=$1 AND funnel_id=$2 RETURNING id""",
                sid, fid, fields["delay_hours"], fields["message_text"],
                fields["button_text"], fields["button_url"])
            if not upd:
                return _err("Шаг не найден", 404)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("auto_funnel_step_update uid=%d step=%d", uid, sid)
            return _err(_INTERNAL_ERROR, 500)

    async def auto_funnel_step_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
            sid = int(request.match_info["step_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор шага", 400)
        if not await _own_funnel(uid, fid):
            return _err("Воронка не найдена", 404)
        try:
            # После удаления номера перенумеровываются подряд: дырка в нумерации
            # оставила бы людей на шаге, которого больше нет, и цепочка встала бы.
            async with pool.acquire() as conn:
                async with conn.transaction():
                    gone = await conn.fetchval(
                        "DELETE FROM auto_funnel_steps WHERE id=$1 AND funnel_id=$2 "
                        "RETURNING step_num", sid, fid)
                    if gone is None:
                        return _err("Шаг не найден", 404)
                    await conn.execute(
                        "UPDATE auto_funnel_steps SET step_num = step_num - 1 "
                        "WHERE funnel_id=$1 AND step_num > $2", fid, int(gone))
                    await conn.execute(
                        "UPDATE auto_funnel_runs SET next_step_num = GREATEST(1, next_step_num - 1) "
                        "WHERE funnel_id=$1 AND next_step_num > $2 AND status='active'",
                        fid, int(gone))
            return _json_resp({"ok": True})
        except Exception:
            log.exception("auto_funnel_step_delete uid=%d step=%d", uid, sid)
            return _err(_INTERNAL_ERROR, 500)

    async def auto_funnel_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        name = (body.get("name") or "").strip()
        bot_id = body.get("bot_id")
        target_segment = (body.get("target_segment") or "all").strip()
        first_message = (body.get("first_message") or "").strip()
        if not name:
            return _err("Нужно название")
        if not bot_id:
            return _err("Нужен идентификатор бота")
        try:
            bot_id = int(bot_id)
        except (TypeError, ValueError):
            return _err("Неверный идентификатор бота")
        bot_row = await pool.fetchrow(
            "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id, uid,
        )
        if not bot_row:
            return _err("Бот не найден", 404)
        try:
            row = await pool.fetchrow(
                "INSERT INTO auto_funnels(owner_id, name, bot_id, target_segment) VALUES($1,$2,$3,$4) RETURNING id",
                uid, name, bot_id, target_segment,
            )
            fid = row["id"]
            if first_message:
                await pool.execute(
                    "INSERT INTO auto_funnel_steps(funnel_id, step_num, delay_hours, message_text) VALUES($1,1,0,$2)",
                    fid, first_message,
                )
            return _json_resp({"ok": True, "id": fid})
        except Exception:
            log.exception("auto_funnel_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def auto_funnel_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        try:
            await pool.execute(
                "DELETE FROM auto_funnels WHERE id=$1 AND owner_id=$2", fid, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("auto_funnel_delete uid=%d fid=%d", uid, fid)
            return _err(_INTERNAL_ERROR, 500)


    # ── Кто именно стоит за числом воронки ──────────────────────────────────
    #
    # Оба экрана воронок показывали ТОЛЬКО числа: «сошли 48», «ждут этого
    # шага: 12», «отвал дальше: 31». Главный вопрос к любой цепочке — кто
    # именно ушёл и на каком шаге, — оставался без ответа: маршрута, который
    # отдал бы этих людей, в продукте не было ни у одной из двух систем
    # воронок. Число без двери — тупик: владелец видит, что цепочка теряет
    # людей, и не может ни посмотреть их, ни написать им.
    #
    # Срез задаётся теми же полями, из которых считалось число на экране,
    # поэтому итог среза совпадает с ним по построению, а не по совпадению.

    async def _funnel_people_names(bot_id: int, uid: int, ids: list[int]) -> dict:
        """Имена подписчиков. Скоуп по ботам владельца, иначе подтянулось бы
        имя из чужого бота с тем же user_id."""
        if not ids:
            return {}
        rows = await _safe_fetch(pool,
            """SELECT DISTINCT ON (bu.user_id) bu.user_id, bu.first_name, bu.username
                 FROM bot_users bu
                 JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                WHERE mb.added_by=$1 AND bu.bot_id=$2
                  AND bu.user_id = ANY($3::bigint[])
             ORDER BY bu.user_id, bu.last_seen DESC NULLS LAST""",
            uid, bot_id, ids)
        return {int(r["user_id"]): dict(r) for r in (rows or [])}

    async def funnel_people(request: web.Request) -> web.Response:
        """Подписчики классической воронки: срез по шагу и по состоянию.

        `current_step` — сколько шагов человеку уже доставлено. Поэтому
        «отвал дальше» на шаге i — это ровно те, у кого current_step = i:
        дошли до i и дальше не пошли. Срез по шагу считает то же самое число,
        что нарисовано на экране.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        funnel = await _safe_fetchrow(pool,
            """SELECT f.id, f.name, f.bot_id
                 FROM funnels f JOIN managed_bots mb ON mb.bot_id=f.bot_id
                WHERE f.id=$1 AND mb.added_by=$2""", funnel_id, uid)
        if not funnel:
            return _err("Воронка не найдена", 404)
        state = (request.query.get("state") or "").strip().lower()
        if state not in ("dropped", "completed", "active"):
            state = ""
        step = validate_integer(request.query.get("step"), min_val=0, max_val=1000)
        limit = _list_limit(request, 100, 500)
        where = ["funnel_id=$1"]
        params: list = [funnel_id]
        if step is not None:
            params.append(step)
            where.append(f"current_step=${len(params)}")
        if state == "dropped":
            where.append("dropped")
        elif state == "completed":
            where.append("completed")
        elif state == "active":
            where.append("NOT completed AND NOT dropped")
        cond = " AND ".join(where)
        try:
            rows = await _safe_fetch(pool,
                f"""SELECT user_id, current_step, completed, dropped,
                           next_send_at, created_at, completed_at
                      FROM funnel_subscriptions WHERE {cond}
                  ORDER BY created_at DESC LIMIT ${len(params) + 1}""",
                *params, limit)
            total = await _safe_count(pool,
                f"SELECT COUNT(*) FROM funnel_subscriptions WHERE {cond}", *params)
        except Exception:
            log.exception("funnel_people uid=%d fid=%d", uid, funnel_id)
            return _err(_INTERNAL_ERROR, 500)
        names = await _funnel_people_names(
            int(funnel["bot_id"]), uid,
            [int(r["user_id"]) for r in (rows or [])])
        items = []
        for r in (rows or []):
            n = names.get(int(r["user_id"]), {})
            items.append({
                "user_id": int(r["user_id"]),
                "name": n.get("first_name"),
                "username": n.get("username"),
                "step": int(r["current_step"] or 0),
                "completed": bool(r["completed"]),
                "dropped": bool(r["dropped"]),
                "next_send_at": (r["next_send_at"].isoformat()
                                 if r["next_send_at"] else None),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
        return _json_resp({
            "funnel": {"id": int(funnel["id"]), "name": funnel["name"]},
            "items": items, "total": int(total or 0),
            "state": state, "step": step, "limit": limit,
        })

    async def auto_funnel_people(request: web.Request) -> web.Response:
        """Прохождения авто-воронки: срез по состоянию и по ожидаемому шагу.

        `status=lost` — сошли: цепочка встала (stopped) или упала с ошибкой
        (error). Экран показывает их одним числом, значит и срез должен быть
        один, иначе число и список разойдутся.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор воронки", 400)
        if not await _own_funnel(uid, fid):
            return _err("Воронка не найдена", 404)
        status = (request.query.get("status") or "").strip().lower()
        if status not in ("active", "completed", "stopped", "error", "lost"):
            status = ""
        step = validate_integer(request.query.get("step"), min_val=1, max_val=1000)
        limit = _list_limit(request, 100, 500)
        where = ["funnel_id=$1"]
        params: list = [fid]
        if status == "lost":
            where.append("status IN ('stopped','error')")
        elif status:
            params.append(status)
            where.append(f"status=${len(params)}")
        if step is not None:
            params.append(step)
            where.append(f"next_step_num=${len(params)}")
        cond = " AND ".join(where)
        try:
            rows = await _safe_fetch(pool,
                f"""SELECT user_id, bot_id, status, next_step_num, next_send_at,
                           last_sent_step, fail_count, started_at
                      FROM auto_funnel_runs WHERE {cond}
                  ORDER BY started_at DESC LIMIT ${len(params) + 1}""",
                *params, limit)
            total = await _safe_count(pool,
                f"SELECT COUNT(*) FROM auto_funnel_runs WHERE {cond}", *params)
        except Exception:
            log.exception("auto_funnel_people uid=%d fid=%d", uid, fid)
            return _err(_INTERNAL_ERROR, 500)
        bot_id = int((rows or [{}])[0]["bot_id"]) if rows else 0
        names = await _funnel_people_names(
            bot_id, uid, [int(r["user_id"]) for r in (rows or [])])
        items = []
        for r in (rows or []):
            n = names.get(int(r["user_id"]), {})
            items.append({
                "user_id": int(r["user_id"]),
                "name": n.get("first_name"),
                "username": n.get("username"),
                "status": r["status"],
                "step": int(r["next_step_num"] or 0),
                "last_sent_step": (int(r["last_sent_step"])
                                   if r["last_sent_step"] is not None else None),
                "fail_count": int(r["fail_count"] or 0),
                "next_send_at": (r["next_send_at"].isoformat()
                                 if r["next_send_at"] else None),
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            })
        return _json_resp({
            "items": items, "total": int(total or 0),
            "status": status, "step": step, "limit": limit,
        })

    app.router.add_put("/api/miniapp/funnel/{funnel_id}/toggle", toggle_funnel)
    app.router.add_get("/api/miniapp/funnels", funnels_all)
    app.router.add_get("/api/miniapp/funnel/{funnel_id}/steps", funnel_steps)
    app.router.add_get("/api/miniapp/funnel/{funnel_id}/people", funnel_people)
    app.router.add_post("/api/miniapp/funnel/{funnel_id}/step", add_funnel_step)
    app.router.add_delete("/api/miniapp/funnel/step/{step_id}", delete_funnel_step)
    app.router.add_patch("/api/miniapp/funnel/step/{step_id}", update_funnel_step)
    app.router.add_get("/api/miniapp/auto_funnels", auto_funnels_list)
    app.router.add_post("/api/miniapp/auto_funnel", auto_funnel_create)
    app.router.add_put("/api/miniapp/auto_funnel/{funnel_id}/toggle", auto_funnel_toggle)
    app.router.add_get("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_detail)
    app.router.add_get("/api/miniapp/auto_funnel/{funnel_id}/people", auto_funnel_people)
    app.router.add_delete("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_delete)
    app.router.add_post("/api/miniapp/auto_funnel/{funnel_id}/step", auto_funnel_step_add)
    app.router.add_put("/api/miniapp/auto_funnel/{funnel_id}/step/{step_id}", auto_funnel_step_update)
    app.router.add_delete("/api/miniapp/auto_funnel/{funnel_id}/step/{step_id}", auto_funnel_step_delete)
