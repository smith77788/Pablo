"""Очередь операций владельца — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 13 маршрутов
`/api/miniapp/operation(s)/*` и 13 функций — список, статус, журнал, отмена,
пауза и снятие с паузы (поштучно и всем сразу), запуск сейчас, повтор
(поштучно и всем упавшим), выгрузка.

Это пульт управления массовыми действиями: через него владелец останавливает
то, что уже пошло по его аккаунтам. Здесь — только разбор запроса, скоуп
владельца и русские ошибки; состояния и переходы живут в
`services/operation_bus.py`, исполнение — в `services/op_worker.py`.
Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import re

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты очереди операций на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, OPERATION_STATUSES, _csv_resp, _err, _get_uid,
        _json, _json_resp, _obus, _retry_sources, _safe_count, _safe_fetch,
        _safe_fetchrow, _safe_fetchval, log, op_status,
    )
    from services.security import validate_integer

    async def operations(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        status_filter = request.query.get("status")
        # 'paused' обязан быть в списке: «Пауза» переводит pending → paused, и без
        # него фильтр по приостановленным молча отдавал ВСЮ очередь — человек
        # видел список и считал, что паузы не было. Неизвестный статус тоже не
        # глотаем: молча проигнорированный фильтр читается как сломанный фильтр.
        if status_filter:
            if status_filter not in OPERATION_STATUSES:
                return _err(
                    "Неизвестный статус операции: " + status_filter, 400)
        # Фильтр по типу операции. Экран массовой публикации раньше просил общую
        # страницу очереди (30 строк) и отбирал mass_publish уже у себя: стоило
        # владельцу запустить тридцать инвайтов после последней публикации — и
        # экран уверенно писал «Нет операций публикации», хотя публикации были.
        # Молча проигнорированный фильтр читается как сломанный, поэтому мусор
        # отклоняем, а не глотаем.
        # Через запятую — для экранов, где один раздел объединяет несколько типов
        # (накрутка: просмотры, реакции, сторис, подписчики, запуски бота).
        _op_raw = (request.query.get("op_type") or "").strip()
        op_type_filter = [t for t in (x.strip() for x in _op_raw.split(",")) if t]
        if len(op_type_filter) > 16:
            return _err("Слишком много типов операций в фильтре", 400)
        for _t in op_type_filter:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", _t):
                return _err("Неизвестный тип операции: " + _t[:40], 400)
        # Поиск по названию операции. У владельца с сотнями операций «тот самый
        # инвайт во вторник» искался перелистыванием: фильтры были только по
        # статусу и по типу, а типов у продукта больше тридцати. Ищем по label
        # (подпись, которую человек и видит в списке) и по op_type — по нему
        # находится вся семья операций, если подписи разные.
        _q = (request.query.get("q") or "").strip()[:64]
        # err_cnt — число упавших под-элементов (каналов) для показа кнопки
        # «повтор неудавшихся» даже у операций со статусом 'done' (partial-fail).
        _err_sub = ("(SELECT COUNT(*) FROM operation_log ol "
                    "WHERE ol.op_id=oq.id AND ol.status='error') AS err_cnt")
        # Страница и честные счётчики. Раньше запрос был жёстко LIMIT 30 без
        # offset и без общего числа: истории дальше тридцати операций просто не
        # существовало, а плитки на экране («Всего», «Готово», «Ошибки»)
        # считались по этим тридцати строкам — у человека с двумя сотнями
        # операций экран уверенно показывал «Всего 30». Значения по умолчанию
        # прежние, так что старый вызов без параметров ведёт себя как раньше.
        try:
            limit = min(
                validate_integer(request.query.get("limit", "30"),
                                 min_val=1, max_val=200) or 30, 200)
        except (ValueError, TypeError):
            limit = 30
        try:
            offset = max(
                validate_integer(request.query.get("offset", "0"),
                                 min_val=0) or 0, 0)
        except (ValueError, TypeError):
            offset = 0
        # Счётчики — по ВСЕЙ очереди владельца, одним проходом, а не по странице.
        # Считаем ДО выборки строк: гейт на фильтр статуса смотрит на последний
        # запрос к operation_queue, и агрегат, вставший после, читался бы там как
        # «фильтр не дошёл до запроса».
        # Счётчики считаются под ТЕМ ЖЕ срезом, что и список. Иначе плитки
        # «Всего / Готово / Ошибки» отвечали бы на другой вопрос, чем список под
        # ними: ищешь «инвайт», видишь три строки и «Всего 214».
        counts: dict = {}
        _cwhere = "owner_id=$1"
        _cargs: list = [uid]
        if op_type_filter:
            _cargs.append(op_type_filter)
            _cwhere += f" AND op_type = ANY(${len(_cargs)}::text[])"
        if _q:
            _cargs.append(f"%{_q}%")
            _cwhere += (f" AND (label ILIKE ${len(_cargs)} "
                        f"OR op_type ILIKE ${len(_cargs)})")
        try:
            _cnt_rows = await pool.fetch(
                f"""SELECT status, COUNT(*) AS n FROM operation_queue
                    WHERE {_cwhere} GROUP BY status""", *_cargs)
            for r in _cnt_rows:
                counts[str(r["status"])] = int(r["n"] or 0)
        except Exception:
            log.debug("operations: счётчики по статусам недоступны uid=%s", uid)
        _cols = f"""oq.id, oq.op_type, oq.status, oq.label, oq.total_items, oq.done_items,
                          {op_status.sql_error_reason("oq.")} AS error_msg,
                          oq.created_at, oq.started_at, oq.finished_at,
                          oq.scheduled_for, {_err_sub}"""
        _where = "oq.owner_id=$1"
        _wargs: list = [uid]
        if status_filter:
            _wargs.append(status_filter)
            _where += f" AND oq.status=${len(_wargs)}"
        if op_type_filter:
            _wargs.append(op_type_filter)
            _where += f" AND oq.op_type = ANY(${len(_wargs)}::text[])"
        if _q:
            _wargs.append(f"%{_q}%")
            _where += (f" AND (oq.label ILIKE ${len(_wargs)} "
                       f"OR oq.op_type ILIKE ${len(_wargs)})")
        rows = await _safe_fetch(pool,
            f"""SELECT {_cols}
               FROM operation_queue oq WHERE {_where}
               ORDER BY oq.created_at DESC, oq.id
               LIMIT ${len(_wargs) + 1} OFFSET ${len(_wargs) + 2}""",
            *_wargs, limit, offset)
        total = await _safe_count(pool,
            f"SELECT COUNT(*) FROM operation_queue oq WHERE {_where}", *_wargs)
        return _json_resp({
            "operations": rows,
            "total": int(total or 0),
            "counts": counts,
            "op_type": (",".join(op_type_filter) if op_type_filter else None),
            "q": (_q or None),
            "page": {"offset": offset, "limit": limit,
                     "has_more": (offset + len(rows or [])) < int(total or 0)},
        })

    async def operation_status(request: web.Request) -> web.Response:
        """Статус одной операции (для инлайн-опроса результата после enqueue)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        row = await _safe_fetchrow(pool,
            "SELECT id, op_type, status, label, total_items, done_items, "
            # Причина честно по ОБЕИМ колонкам: error_msg — терминальный
            # провал, last_error — почему операция ждёт (флуд-пауза, повторная
            # попытка, рестарт воркера). Читая только первую, экран молчал о
            # том, что операция отложена Telegram на час.
            f"{op_status.sql_error_reason()} AS error_msg, "
            "created_at, finished_at, scheduled_for, "
            "COALESCE(result->>'summary', result->>'reason') AS summary, "
            "result AS _result, params AS _params "
            "FROM operation_queue WHERE id=$1 AND owner_id=$2", op_id, uid)
        if not row:
            return _err("Операция не найдена", 404)
        d = dict(row)
        # Быстрые действия по ИТОГУ: раньше сводка советовала словами
        # («назначьте прокси», «проверьте права», «повторите позже»), а кнопки
        # рядом не было — совет уходил в пустоту.
        _res_raw = d.pop("_result", None)
        _prm_raw = d.pop("_params", None)
        try:
            import json as _json_qa
            from services import op_quick_actions as _qa

            _res = (_res_raw if isinstance(_res_raw, dict)
                    else _json_qa.loads(_res_raw or "{}"))
            _prm = (_prm_raw if isinstance(_prm_raw, dict)
                    else _json_qa.loads(_prm_raw or "{}"))
            d["quick_actions"] = _qa.suggest(
                d.get("op_type"), d.get("status"), _res, op_id=op_id, params=_prm)
        except Exception:
            log.debug("operation_status: быстрые действия недоступны op=%s", op_id)
            d["quick_actions"] = []
        # ISO для фронта (детали операции: Создано/Завершено/Запланировано)
        for _k in ("created_at", "finished_at", "scheduled_for"):
            if d.get(_k) is not None and hasattr(d[_k], "isoformat"):
                d[_k] = d[_k].isoformat()
        # Влияние операции на аккаунты: какие аккаунты словили FloodWait за эту
        # операцию (account_flood_log.operation_id). Замыкает петлю «операция → её
        # след на аккаунтах». Owner-scoped через join. Fail-soft.
        try:
            imp = await _safe_fetch(pool,
                """SELECT a.id, a.phone, a.first_name, a.acc_status,
                          COUNT(*) AS floods, MAX(f.actual_wait) AS max_wait
                   FROM account_flood_log f
                   JOIN tg_accounts a ON a.id = f.account_id
                   WHERE f.operation_id=$1 AND a.owner_id=$2
                   GROUP BY a.id, a.phone, a.first_name, a.acc_status
                   ORDER BY floods DESC LIMIT 20""", op_id, uid)
            d["accounts_impact"] = [dict(r) for r in (imp or [])]
        except Exception as _e:
            log.debug("operation_status impact op=%s: %s", op_id, _e)
            d["accounts_impact"] = []
        return _json_resp(d)

    async def operation_log(request: web.Request) -> web.Response:
        """Пошаговый лог одной операции (per-target: канал/бот/аккаунт → статус).

        Фронт (openOpDetail) рендерит секцию «📋 Лог» из этого ответа. Раньше
        маршрут не был зарегистрирован → api() ловил 404 через .catch и секция
        лога ВСЕГДА была пустой (мёртвая кнопка). Скоуп по owner_id обязателен —
        иначе можно прочитать лог чужой операции по её id.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        # Проверяем владение операцией перед чтением её лога.
        owns = await _safe_fetchrow(pool,
            "SELECT 1 FROM operation_queue WHERE id=$1 AND owner_id=$2", op_id, uid)
        if not owns:
            return _err("Операция не найдена", 404)
        # Лог отдавался одним куском на 500 строк, а фронт писал в заголовок его
        # длину: у операции на 2000 целей секция называлась «Лог (500)» и это
        # выдавалось за весь лог. Найти среди них неудачные цели было нечем —
        # а ради них на лог и смотрят. Теперь: страница, общее число и счётчики
        # по статусам, плюс фильтр по статусу.
        try:
            limit = min(validate_integer(request.query.get("limit", "200"),
                                         min_val=1, max_val=1000) or 200, 1000)
        except (ValueError, TypeError):
            limit = 200
        try:
            offset = max(validate_integer(request.query.get("offset", "0"),
                                          min_val=0, max_val=1000000) or 0, 0)
        except (ValueError, TypeError):
            offset = 0
        status = (request.query.get("status") or "").strip().lower()
        if status not in ("ok", "fail", "failed", "error", "skip", "skipped"):
            status = ""
        # Поиск по цели. У операции на 2000 целей вопрос «а @ivan-то позвали?»
        # иначе решается прокруткой. Ищем и по сообщению: причина отказа
        # («Privacy restricted») — второй способ найти нужные строки.
        needle = (request.query.get("q") or "").strip()[:64].lstrip("@").strip()

        # Счётчики и общее число считаются ПО ТОЙ ЖЕ выборке, что и строки:
        # разойдись они — шапка «N из M» описывала бы один набор, а список
        # показывал другой.
        _qc = " AND (target ILIKE $2 OR message ILIKE $2)" if needle else ""
        _qa: list = [f"%{needle}%"] if needle else []
        counts: dict = {}
        total = 0
        try:
            agg = await pool.fetch(
                "SELECT status, COUNT(*) AS n FROM operation_log "
                f"WHERE op_id=$1{_qc} GROUP BY status", op_id, *_qa)
            for r in agg:
                counts[(r["status"] or "").lower() or "unknown"] = int(r["n"] or 0)
            total = sum(counts.values())
        except Exception as exc:
            log.warning("operation_log op=%d: агрегат недоступен: %s", op_id, exc)
            total = await _safe_count(
                pool, f"SELECT COUNT(*) FROM operation_log WHERE op_id=$1{_qc}",
                op_id, *_qa)

        _n = len(_qa)
        if status:
            rows = await _safe_fetch(pool,
                "SELECT step_num, target, status, message, created_at "
                f"FROM operation_log WHERE op_id=$1{_qc} AND lower(status)=${_n + 2} "
                f"ORDER BY step_num, id LIMIT ${_n + 3} OFFSET ${_n + 4}",
                op_id, *_qa, status, limit, offset)
            shown_total = counts.get(status, len(rows) + offset)
        else:
            rows = await _safe_fetch(pool,
                "SELECT step_num, target, status, message, created_at "
                f"FROM operation_log WHERE op_id=$1{_qc} "
                f"ORDER BY step_num, id LIMIT ${_n + 2} OFFSET ${_n + 3}",
                op_id, *_qa, limit, offset)
            shown_total = total
        return _json_resp({
            "logs": rows,
            "total": total,
            "counts": counts,
            "status": status,
            "q": needle or None,
            "page": {"offset": offset, "limit": limit,
                     "has_more": offset + len(rows) < shown_total},
        })

    async def cancel_operation(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        try:
            # 'paused' обязателен: приостановленную операцию иначе нельзя было ни
            # отменить, ни снять поштучно — она навсегда висела в очереди, пока
            # пользователь не возобновит ВСЮ очередь целиком.
            row = await _obus.cancel(
                pool, op_id, uid,
                allow=("pending", "running", "paused"))
            if not row:
                return _err("Операция не найдена или уже завершена", 404)
            return _json_resp({"ok": True})
        except Exception as e:
            log.warning("cancel_operation op=%d uid=%d: %s", op_id, uid, e)
            return _err("Не удалось отменить операцию", 500)

    async def pause_operation(request: web.Request) -> web.Response:
        """Приостановить ОДНУ операцию: pending → paused.

        Раньше пауза была только на всю очередь, а на экране деталей кнопка
        «⏸ Приостановить» звала cancel — операция уходила в 'cancelled'
        безвозвратно, хотя подпись обещала обратимое действие.

        Запущенную (running) не паузим: без чекпоинта пере-прогон продублировал
        бы уже сделанные действия и сжёг аккаунты — та же причина, что и у
        очереди целиком (см. pause_operations).
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        try:
            row = await pool.fetchrow(
                # acct_wait_since=NULL: это отметка начала ожидания свободных
                # аккаунтов, по которой op_worker проваливает операцию через
                # _ACCT_WAIT_MAX_MIN. Пауза владельца — не ожидание флота, и
                # время под паузой в этот предел идти не должно. Без сброса
                # операция, постоявшая на паузе дольше двадцати минут,
                # проваливалась на ПЕРВОЙ же встрече с занятым флотом после
                # возобновления, не прождав ни секунды, — ровно тот класс,
                # который однажды уже чинили для отложенного старта.
                """UPDATE operation_queue SET status='paused', acct_wait_since=NULL
                   WHERE id=$1 AND owner_id=$2 AND status='pending'
                   RETURNING id""", op_id, uid)
            if not row:
                # Разделяем «нет такой» и «уже запущена»: пользователю важно
                # понимать, почему кнопка не сработала.
                cur = await _safe_fetchval(
                    pool, "SELECT status FROM operation_queue "
                          "WHERE id=$1 AND owner_id=$2", op_id, uid)
                if cur is None:
                    return _err("Операция не найдена", 404)
                if cur == "running":
                    # ТЕКУЩИЙ ПРОХОД НЕ РВЁМ, НАМЕРЕНИЕ ЗАПОМИНАЕМ. Прервать
                    # идущую операцию без чекпоинта означало бы пере-прогон и
                    # дубли реальных действий. Но проход заканчивается сам
                    # (деплой, сброс зависшей, отсрочка по флуд-паузе), и
                    # раньше операция после этого запускалась ЗАНОВО, хотя
                    # владелец нажал паузу. Теперь на следующем входе в работу
                    # поллер переведёт её в 'paused'. Выбор «только отменить»
                    # стоил владельцу уже сделанной работы.
                    await pool.execute(
                        "UPDATE operation_queue SET pause_requested=TRUE "
                        "WHERE id=$1 AND owner_id=$2 AND status NOT IN "
                        f"{op_status.sql_terminal_list()}", op_id, uid)
                    return _json_resp({
                        "ok": True, "status": "running", "pause_requested": True,
                        "message": "Операция уже идёт. Текущий проход доиграет, "
                                   "и дальше она встанет на паузу, а не начнётся "
                                   "заново.",
                    })
                return _err(f"Операцию нельзя приостановить в статусе «{cur}»", 409)
            return _json_resp({"ok": True, "status": "paused"})
        except Exception as e:
            log.warning("pause_operation op=%d uid=%d: %s", op_id, uid, e)
            return _err("Не удалось приостановить операцию", 500)

    async def resume_operation(request: web.Request) -> web.Response:
        """Возобновить ОДНУ операцию: paused → pending (воркер подхватит).

        ВРЕМЯ ЗАПУСКА НЕ ТРОГАЕМ. Раньше «возобновить» заодно снимало
        scheduled_for — как быстрый способ поторопить операцию, отложенную
        живой очередью. С тех пор для этого появилась отдельная кнопка
        «Запустить сейчас» (run_now_operation ниже), а побочный эффект у
        «возобновить» остался, и он ломал два сценария.

        1. Запланированное время владельца пропадало. Операция, заведённая на
           «завтра в 9:00», после паузы и возобновления уходила в работу
           НЕМЕДЛЕННО: пост в реальные каналы в три часа ночи, отменить уже
           нечего. Владелец при этом ничего такого не просил — он нажал
           «возобновить», а не «запустить сейчас».
        2. Отложенность, защищающая аккаунты, снималась в обход. Флуд-пауза и
           перенос при занятом флоте живут в том же scheduled_for, и пара
           «пауза → возобновить» отправляла аккаунт обратно в Telegram раньше
           срока, который Telegram сам и назначил.

        Возобновление возвращает операцию ровно в то состояние, из которого её
        приостановили. Поторопить её — явное отдельное действие. Массовое
        возобновление (resume_operations) вело себя так всегда; теперь обе
        кнопки значат одно и то же.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        try:
            row = await pool.fetchrow(
                # acct_wait_since=NULL — и на возобновлении тоже: отметку могла
                # поставить пауза, сделанная до этой правки, а провалить
                # возобновлённую операцию за «ждёт слишком долго» нельзя.
                # pause_requested=FALSE — намерение снято явным «Старт».
                # Иначе возобновлённая операция снова встала бы на паузу на
                # первом же тике поллера: флаг у неё мог остаться с паузы,
                # нажатой пока она ещё шла.
                """UPDATE operation_queue
                      SET status='pending', acct_wait_since=NULL,
                          pause_requested=FALSE
                   WHERE id=$1 AND owner_id=$2 AND status='paused'
                   RETURNING id""", op_id, uid)
            if not row:
                cur = await _safe_fetchval(
                    pool, "SELECT status FROM operation_queue "
                          "WHERE id=$1 AND owner_id=$2", op_id, uid)
                if cur is None:
                    return _err("Операция не найдена", 404)
                # Идущая операция с заказанной паузой: «Старт» означает «не
                # надо, пусть работает дальше» — снимаем намерение.
                if cur == "running":
                    _unmarked = await pool.fetchval(
                        "UPDATE operation_queue SET pause_requested=FALSE "
                        "WHERE id=$1 AND owner_id=$2 AND pause_requested "
                        "RETURNING id", op_id, uid)
                    if _unmarked:
                        return _json_resp({
                            "ok": True, "status": "running",
                            "pause_requested": False,
                            "message": "Пауза отменена — операция продолжит "
                                       "работу.",
                        })
                return _err(f"Операция не приостановлена (статус «{cur}»)", 409)
            return _json_resp({"ok": True, "status": "pending"})
        except Exception as e:
            log.warning("resume_operation op=%d uid=%d: %s", op_id, uid, e)
            return _err("Не удалось возобновить операцию", 500)

    async def run_now_operation(request: web.Request) -> web.Response:
        """Снять scheduled_for у операции в 'pending' — поллер (тик раз в
        _POLL_INTERVAL=10с) подхватит её на ближайшем цикле вместо того, чтобы
        ждать исходное время.

        Раньше единственный способ «поторопить» операцию, автоматически
        отложенную живой очередью (флот был занят другой операцией —
        _requeue_op_no_accounts выставляет scheduled_for = now() + до 90с, при
        долгом ожидании — больше), был приостановить и снова возобновить
        (см. resume_operation) — не очевидно и требовало двух действий вместо
        одного явного «запустить сейчас»."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE operation_queue SET scheduled_for=NULL
                   WHERE id=$1 AND owner_id=$2 AND status='pending'
                   RETURNING id""", op_id, uid)
            if not row:
                cur = await _safe_fetchval(
                    pool, "SELECT status FROM operation_queue "
                          "WHERE id=$1 AND owner_id=$2", op_id, uid)
                if cur is None:
                    return _err("Операция не найдена", 404)
                return _err(f"Операцию нельзя запустить сейчас в статусе «{cur}»", 409)
            return _json_resp({"ok": True, "status": "pending"})
        except Exception as e:
            log.warning("run_now_operation op=%d uid=%d: %s", op_id, uid, e)
            return _err("Не удалось запустить операцию сейчас", 500)

    async def clear_operations(request: web.Request) -> web.Response:
        """Очистить очередь: УДАЛИТЬ терминальные операции (done/failed/cancelled)
        владельца.

        Раньше кнопка «Очистить» на фронте слала cancel по каждой done-операции —
        а cancel_operation бьёт только по status IN ('pending','running'), т.е. для
        завершённых это no-op (404). Тост показывал число ВЫБРАННЫХ, а не реально
        удалённых → «Очищено 30», но список оставался полным. Здесь реальный DELETE
        (operation_log удаляется каскадом ON DELETE CASCADE), возвращаем факт.

        pending/running НЕ трогаем (активные операции). Скоуп по owner_id обязателен.

        ЧЕГО ЕЩЁ НЕ ТРОГАЕМ: завершённую операцию, на журнал которой опирается
        живой повтор. Журнал (operation_log) висит на строке очереди внешним
        ключом ON DELETE CASCADE, то есть удаление строки уносит и его. А повтор
        — это НОВАЯ операция, которая пропускает уже сделанное именно по журналу
        исходной (op_worker.journal_op_ids). Последовательность «повторить, а
        пока подчищу список» вполне естественная, и она стирала память о
        сделанном: повтор рассылки, вставшей на 203 адресатах из 380, уходил бы
        по всем 380 заново. Такая строка остаётся в списке до конца повтора.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            res = await pool.execute(
                "DELETE FROM operation_queue oq "
                # Все ТЕРМИНАЛЬНЫЕ (done/partial/failed/cancelled) — чтобы «Частично»
                # тоже чистилось. Раньше partial не входил, и частичные операции
                # нельзя было убрать из списка, только перезапустить (жалоба
                # владельца). Единый источник статусов — op_status.
                f"WHERE oq.owner_id=$1 AND oq.status IN {op_status.sql_terminal_list()} "
                "  AND NOT EXISTS (SELECT 1 FROM operation_queue r "
                "                   WHERE r.owner_id = oq.owner_id "
                f"                    AND r.status IN {op_status.sql_in_flight_list()} "
                "                     AND r.params->>'retry_of_op' = oq.id::text)", uid)
            tail = str(res).rsplit(" ", 1)[-1]
            deleted = int(tail) if tail.isdigit() else 0
            return _json_resp({"ok": True, "deleted": deleted})
        except Exception as e:
            log.warning("clear_operations uid=%d: %s", uid, e)
            return _err("Не удалось очистить", 500)

    async def pause_operations(request: web.Request) -> web.Response:
        """Приостановить ОЧЕРЕДЬ: pending → paused.

        Честная пауза (раньше «Пауза» слала cancel по running → операции гибли в
        'cancelled', возобновить их «Старт» не мог). Воркер подхватывает только
        status='pending' (см. op_worker pickup) — значит paused не исполняется и не
        сбрасывается stale-логикой. Уже запущенные (running) доигрывают: паузить их
        нельзя без чекпоинта — пере-прогон дублировал бы действия и палил аккаунты.
        Скоуп по owner_id. Возвращаем реальное число приостановленных.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            res = await pool.execute(
                # acct_wait_since=NULL — см. пояснение у паузы одной операции:
                # время под паузой не идёт в предел ожидания свободного флота.
                "UPDATE operation_queue SET status='paused', acct_wait_since=NULL "
                "WHERE owner_id=$1 AND status='pending'", uid)
            tail = str(res).rsplit(" ", 1)[-1]
            paused = int(tail) if tail.isdigit() else 0
            # Идущие доигрывают текущий проход — и дальше встают на паузу, а не
            # начинаются заново. Без этой отметки «Пауза» отпускалась на
            # первом же деплое или сбросе зависшей (см. schema_v249).
            marked = 0
            try:
                _m = await pool.execute(
                    "UPDATE operation_queue SET pause_requested=TRUE "
                    f"WHERE owner_id=$1 AND status IN {op_status.sql_in_flight_list()} "
                    "AND NOT pause_requested", uid)
                _mt = str(_m).rsplit(" ", 1)[-1]
                marked = int(_mt) if _mt.isdigit() else 0
            except Exception as _me:
                log.warning("pause_operations mark uid=%d: %s", uid, _me)
            return _json_resp({"ok": True, "paused": paused,
                               "pause_requested": marked})
        except Exception as e:
            log.warning("pause_operations uid=%d: %s", uid, e)
            return _err("Не удалось приостановить", 500)

    async def resume_operations(request: web.Request) -> web.Response:
        """Возобновить приостановленные: paused → pending (воркер подхватит).

        Возвращает в очередь ИМЕННО приостановленные операции — паритет с «Пауза».
        Скоуп по owner_id, реальное число возобновлённых.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            res = await pool.execute(
                "UPDATE operation_queue SET status='pending', acct_wait_since=NULL, "
                "pause_requested=FALSE "
                "WHERE owner_id=$1 AND status='paused'", uid)
            tail = str(res).rsplit(" ", 1)[-1]
            resumed = int(tail) if tail.isdigit() else 0
            # Снять намерение и с тех, кто всё это время шёл: «Старт» значит
            # «работаем дальше», иначе они встали бы на паузу на следующем
            # тике поллера — уже после того, как владелец её отменил.
            unmarked = 0
            try:
                _u = await pool.execute(
                    "UPDATE operation_queue SET pause_requested=FALSE "
                    "WHERE owner_id=$1 AND pause_requested", uid)
                _ut = str(_u).rsplit(" ", 1)[-1]
                unmarked = int(_ut) if _ut.isdigit() else 0
            except Exception as _ue:
                log.warning("resume_operations unmark uid=%d: %s", uid, _ue)
            # unmarked — сколько идущих операций перестали ждать паузы: без
            # этого числа интерфейс говорил «нет приостановленных», хотя
            # тормоз с идущих он только что снял.
            return _json_resp({"ok": True, "resumed": resumed,
                               "unmarked": unmarked})
        except Exception as e:
            log.warning("resume_operations uid=%d: %s", uid, e)
            return _err("Не удалось возобновить", 500)

    async def retry_operation(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор операции", 400)
        try:
            row = await pool.fetchrow(
                "SELECT op_type, params, label, status, total_items, done_items, "
                "(SELECT COUNT(*) FROM operation_log ol "
                " WHERE ol.op_id=operation_queue.id AND ol.status='error') AS err_cnt "
                "FROM operation_queue WHERE id=$1 AND owner_id=$2",
                op_id, uid)
            if not row:
                return _err("Не найдено", 404)

            # Публикация: повторяем ТОЛЬКО упавшие каналы. Иначе успешные каналы
            # получили бы пост повторно (дубликаты). Работает и для
            # partial-success ('done' с ошибками) — паритет с ботом.
            #
            # Список упавших целей собирает operation_bus.collect_failed_targets,
            # а не запрос на месте. Тот запрос брал ВСЕ строки со status='error'
            # и не смотрел, не закрылась ли цель успехом позже: канал, упавший на
            # первой попытке и опубликованный на второй, попадал в «упавшие», и
            # повтор пытался опубликовать в него снова. Общий сборщик такие цели
            # исключает и строго разбирает формат target, отбрасывая мусор.
            if row["op_type"] in ("mass_publish", "quick_post"):
                failed_ids = [
                    int(x) for x in await _obus.collect_failed_targets(
                        pool, op_id, row["op_type"])
                ]
                if not failed_ids:
                    return _err("Нет неудавшихся каналов для повтора", 400)
                try:
                    base = row["params"] if isinstance(row["params"], dict) else _json.loads(row["params"] or "{}")
                except (TypeError, ValueError):
                    base = {}
                # Связь с исходной операцией: по ней исполнитель видит её журнал
                # и не делает второй раз то, что там уже помечено сделанным
                # (op_worker.journal_op_ids). Тот же сборщик снимает ключи
                # расписания — повтор круга автопостинга не должен заводить
                # вторую цепочку публикаций (operation_bus.params_for_retry).
                base = _obus.params_for_retry(op_id, base)
                base["channel_ids"] = failed_ids
                new_id = await _obus.submit(
                    pool, uid, row["op_type"], base,
                    total_items=len(failed_ids),
                    label=f"Повтор неудавшихся ({len(failed_ids)})")
                return _json_resp({"ok": True, "new_id": new_id, "channels": len(failed_ids)})

            # Прочие операции — повтор целиком. «Упавшая» для человека это не
            # статус в базе, а НЕДОВЕДЁННАЯ работа: инвайт останавливался на
            # 203 целях из 380 со статусом `done`, и повторить его было нечем.
            from services import operation_retry as _oretry

            _can, _why = _oretry.can_retry(
                row["status"], row["done_items"], row["total_items"], row["err_cnt"])
            if not _can:
                return _err(f"Повтор невозможен: {_why}", 409)
            try:
                _retry_params = (row["params"] if isinstance(row["params"], dict)
                                 else _json.loads(row["params"] or "{}"))
            except (TypeError, ValueError):
                _retry_params = {}
            # Связь с исходной операцией. Повтор — это НОВАЯ строка очереди с
            # новым id, а журнал уже сделанной работы лежит под СТАРЫМ id. Без
            # этой ссылки исполнитель начинал со свежим пустым журналом и честно
            # проходил весь список целей заново: рассылка, вставшая на 203
            # адресатах из 380, присылала этим 203 второе одинаковое сообщение,
            # пост уходил второй раз в каналы, которые его уже получили, а
            # создание каналов давало второй комплект. По ссылке исполнитель
            # видит журнал предка (op_worker.journal_op_ids) и доделывает
            # остаток, а не переделывает всё.
            # Тот же сборщик снимает ключи расписания: «Повторить» означает
            # «сделай эту работу ещё раз», а не «заведи второе расписание».
            _recurring = _obus.drops_recurrence(_retry_params)
            _retry_params = _obus.params_for_retry(op_id, _retry_params)
            # Метку «↻» ставит планировщик кругов; у разового повтора расписания
            # нет, и метка врала бы владельцу прямо в очереди.
            _retry_label = row["label"]
            if _recurring and isinstance(_retry_label, str) and _retry_label.endswith(" ↻"):
                _retry_label = _retry_label[:-2].rstrip()
            # Окно идемпотентности НЕ отключаем. Раньше отключали: повтор
            # считался «той же операцией с теми же params», и окно приняло бы
            # его за двойной тап. Обе половины этого рассуждения больше не
            # верны. Во-первых, дедуп ищет совпадение только среди ЖИВЫХ
            # операций ('pending'/'running'), а повторить можно лишь
            # недоведённую — то есть завершённую (operation_retry.can_retry
            # прямо отказывает операции в работе). Во-вторых, params повтора
            # несут ссылку retry_of_op, которой у исходной операции нет, так
            # что и по содержимому они уже различаются.
            #
            # Зато отключённое окно снимало защиту от двойного тапа именно там,
            # где по кнопке бьют чаще всего: владелец видит, что работа не
            # доведена, и жмёт «Повторить» дважды. Получались ДВЕ одинаковые
            # операции — двойная рассылка тем же адресатам, двойной пост в те же
            # каналы, двойной расход лимитов аккаунтов.
            # Размер повтора — ОСТАТОК работы, а не потолок предка. С
            # унаследованным потолком повтор, доделавший всё (177 целей из 380,
            # где 203 уже закрыты), по недобору прогресса объявлялся «частично
            # выполненным» — и его снова предлагали повторить: круг ложных
            # «недоведено» (operation_bus._retry_total_items).
            _retry_total = await _obus._retry_total_items(
                pool, op_id, row["total_items"])
            if int(row["total_items"] or 0) > 0 and _retry_total == 0:
                return _err("Повторять нечего: все цели уже закрыты", 409)
            try:
                new_id = await _obus.submit(
                    pool, uid, row["op_type"], _retry_params,
                    total_items=_retry_total, label=_retry_label)
            except ValueError:
                # Тип операции больше не поддерживается (переименован или убран из
                # реестра). Раньше копирование строки очереди воскрешало такую
                # операцию, и она навсегда зависала pending — воркер её не знает.
                return _err(
                    f"Тип операции «{row['op_type']}» больше не поддерживается — "
                    "повтор невозможен", 400)
            _resp = {"ok": True, "new_id": new_id}
            if _recurring:
                _resp["note"] = ("Повтор запущен один раз — расписание исходной "
                                 "операции не копируется.")
            return _json_resp(_resp)
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("retry_operation op_id=%d uid=%d", op_id, uid)
            return _err("Не удалось повторить", 500)

    async def operations_retry_failed(request: web.Request) -> web.Response:
        """Перезапустить все недоведённые операции разом.

        Раньше повтор был только поштучным и только для `failed`. Операция,
        закрывшаяся `done` без достижения цели (инвайт: 203 из 380), не
        перезапускалась вообще, а разбирать очередь по одной руками — не работа.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import operation_retry as _oretry

        rows = await _safe_fetch(pool,
            """SELECT oq.id, oq.op_type, oq.label, oq.status,
                      oq.done_items, oq.total_items,
                      (SELECT COUNT(*) FROM operation_log ol
                        WHERE ol.op_id=oq.id AND ol.status='error') AS err_cnt
                 FROM operation_queue oq
                WHERE oq.owner_id=$1
                  AND oq.status IN ('failed','cancelled','done')
                  AND oq.created_at > now() - INTERVAL '7 days'
                  -- Уже перезапущенные из выборки исключаются, иначе каждое
                  -- нажатие брало ОДНИ И ТЕ ЖЕ двадцать пять новейших: дедуп
                  -- превращал их в ничто, остальные недоведённые не брались
                  -- никогда, и «нажмите ещё раз» было бы неправдой.
                  AND NOT EXISTS (
                        SELECT 1 FROM operation_queue r
                         WHERE r.owner_id=$1
                           AND r.params->>'retry_of_op' = oq.id::text)
                ORDER BY oq.created_at DESC, oq.id DESC
                LIMIT 300""", uid)
        candidates = _oretry.pick_retryable([dict(r) for r in (rows or [])])
        if not candidates:
            return _json_resp({"ok": True, "retried": 0,
                               "note": "Недоведённых операций за неделю нет"})
        import json as _json2

        retried, skipped = [], []
        dropped_schedules = 0
        chosen = candidates[:25]    # потолок: не заваливаем очередь одним нажатием
        # Параметры всех выбранных операций — ОДНИМ запросом. Раньше здесь был
        # отдельный запрос на каждую: до 25 обращений к базе подряд, и владелец
        # ждал их все, прежде чем увидеть результат нажатия.
        _srcs = await _retry_sources(pool, uid, [int(c["id"]) for c in chosen])
        for c in chosen:
            src = _srcs.get(int(c["id"]))
            if not src:
                continue
            try:
                prm = (src["params"] if isinstance(src["params"], dict)
                       else _json2.loads(src["params"] or "{}"))
            except (TypeError, ValueError):
                prm = {}
            # Общий сборщик params повтора: ставит ссылку на журнал предка (иначе
            # массовый повтор переделывал ВСЮ уже сделанную работу — до 25
            # операций за нажатие) и снимает ключи расписания. Без этого одно
            # нажатие размножало автопостинг в 25 параллельных цепочек: у круга
            # с часовым интервалом за неделю набирается больше сотни записей в
            # очереди, и каждая тащила свой `repeat_interval_min`.
            _had_schedule = _obus.drops_recurrence(prm)
            prm = _obus.params_for_retry(int(c["id"]), prm)
            _lbl = str(src["label"] or src["op_type"])
            if _lbl.endswith(" ↻"):
                _lbl = _lbl[:-2].rstrip()
            try:
                # Окно идемпотентности не отключаем — см. разбор в
                # retry_operation выше. Здесь цена двойного тапа выше всего:
                # кнопка перезапускает до 25 операций за нажатие, то есть два
                # тапа давали 50. Каждая операция дедупится по своей ссылке
                # retry_of_op, поэтому разные операции друг с другом не
                # сливаются, а повторное нажатие сливается само с собой.
                new_id = await _obus.submit(
                    pool, uid, src["op_type"], prm,
                    total_items=src["total_items"] or 0,
                    label=f"Повтор: {_lbl}")
                retried.append({"from": int(c["id"]), "new_id": new_id,
                                "reason": c["reason"]})
                # Считаем только реально поставленные: иначе цифра в ответе
                # обещала бы владельцу запуски, которых не было.
                if _had_schedule:
                    dropped_schedules += 1
            except PermissionError as exc:
                skipped.append({"id": int(c["id"]), "why": str(exc)[:80]})
            except ValueError:
                skipped.append({"id": int(c["id"]),
                                "why": f"тип «{src['op_type']}» больше не поддерживается"})
        # Если по тарифу отказано ВО ВСЁМ — это отказ, а не «успех с пропусками»:
        # честный 403 с причиной, иначе платное ограничение выглядело бы как
        # молчаливое ничегонеделание. Частичный успех остаётся успехом.
        if not retried and skipped:
            return _err("Повтор отклонён тарифом: " + (skipped[0]["why"] or "требуется подписка"), 403)
        # Кнопка называется «перезапустить все», а за нажатие ставится не
        # больше двадцати пяти: иначе одно нажатие заваливает очередь. Молчать
        # об остатке нельзя — владелец читает «Перезапущено 25» как «всё», и
        # недоведённые операции так и остаются недоведёнными.
        left = max(0, len(candidates) - len(chosen))
        return _json_resp({
            "ok": True, "retried": len(retried), "skipped": len(skipped),
            "candidates": len(candidates), "left": left, "items": retried[:25],
            "note": (f"Перезапущено {len(retried)}"
                     + (f", пропущено {len(skipped)}" if skipped else "")
                     + (f". Разовым запуском, без копирования расписания: "
                        f"{dropped_schedules}" if dropped_schedules else "")
                     + (f". Осталось недоведённых: {left} — нажмите ещё раз"
                        if left else "")),
        })

    async def operation_export(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        fmt = request.query.get("format", "csv")
        try:
            # Потолок — предохранитель от выгрузки на миллион строк, а не
            # продуктовое ограничение. Было 500: владелец скачивал
            # operations.csv, считая, что это история, а получал последние
            # полтораста часов работы. Выгрузку несут из приложения наружу — в
            # отчёт, в таблицу, в переписку, — и обрезанная выгрузка хуже
            # отказа: по ней принимают решения, не зная, что её обрезали.
            cap = 50_000
            rows = await pool.fetch(
                # Колонки err_cnt нет — экспорт падал всегда. Число неудачных
                # попыток в этой схеме — retry_count.
                """SELECT id, op_type, label, status, total_items, done_items,
                          COALESCE(retry_count, 0) AS err_cnt, created_at, finished_at
                   FROM operation_queue WHERE owner_id=$1
                   ORDER BY created_at DESC, id DESC LIMIT $2""",
                uid, cap,
            )
            if fmt == "json":
                total = await _safe_count(pool,
                    "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1", uid)
                return _json_resp({"operations": [dict(r) for r in rows],
                                   "total": total,
                                   "truncated": len(rows) >= cap})
            header = ["ID", "Тип", "Метка", "Статус", "Всего", "Сделано", "Ошибок", "Создано", "Завершено"]
            data = [[
                r["id"], r["op_type"], r["label"] or "", r["status"],
                r["total_items"] or 0, r["done_items"] or 0, r["err_cnt"] or 0,
                str(r["created_at"] or ""), str(r["finished_at"] or ""),
            ] for r in rows]
            return _csv_resp("operations.csv", header, data)
        except Exception:
            log.exception("operation_export")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/operations", operations)
    app.router.add_get("/api/miniapp/operation/{op_id}", operation_status)
    app.router.add_get("/api/miniapp/operation/{op_id}/log", operation_log)
    app.router.add_post("/api/miniapp/operation/{op_id}/cancel", cancel_operation)
    app.router.add_post("/api/miniapp/operation/{op_id}/pause", pause_operation)
    app.router.add_post("/api/miniapp/operation/{op_id}/resume", resume_operation)
    app.router.add_post("/api/miniapp/operation/{op_id}/run_now", run_now_operation)
    app.router.add_post("/api/miniapp/operation/{op_id}/retry", retry_operation)
    app.router.add_post("/api/miniapp/operations/clear", clear_operations)
    app.router.add_post("/api/miniapp/operations/pause", pause_operations)
    app.router.add_post("/api/miniapp/operations/resume", resume_operations)
    app.router.add_post("/api/miniapp/operations/retry_failed", operations_retry_failed)
    app.router.add_get("/api/miniapp/operations/export", operation_export)
