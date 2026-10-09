"""Сценарии автоматизации (воркфлоу) мини-аппа.

Вынесено из `services/mini_app_api.py`: тот файл — больше 25 тысяч строк в
одной функции `setup_routes`, и двенадцать маршрутов сценариев лежали в нём
двумя разными кусками, в двух тысячах строк друг от друга. Именно поэтому они
и разъехались.

ЧТО БЫЛО РАЗЪЕХАВШИМСЯ. На каждое действие со сценарием здесь жили ДВЕ
реализации: аккуратная в `services/workflow_engine.py` и переписанная заново
рядом с маршрутом. Кнопка в мини-аппе нажимала наивную.

  * Удаление. Экран зовёт `DELETE /workflows/{id}` — там стоял голый
    `DELETE FROM workflow_definitions` с безусловным `{"ok": true}`. Удаление
    чужого или уже удалённого сценария отвечало «🗑 Удалён», после чего список
    перезагружался и показывал сценарий на месте. Хуже: `workflow_runs.workflow_id`
    ссылается на определение внешним ключом БЕЗ `ON DELETE`, так что у сценария
    с прогонами этот запрос падает ошибкой БД. Готовый `delete_workflow` в
    движке именно это и разбирает: отменяет незавершённые прогоны, отвязывает
    остальные, возвращает False, если у владельца такого сценария нет. Его
    звал только `DELETE /workflow/{id}` — маршрут, которого нет во фронте.
  * Пауза и активация. `PATCH /workflows/{id}` переписывал тот же UPDATE, что
    и `_set_active` в движке, со своим ответом.
  * Создание. `POST /workflows` писал свой INSERT.

ЧТО БЫЛО С ЯЗЫКОМ ШАГОВ. Экран умеет рисовать шаг `{"type": ...}` и только его
(`s.type.toUpperCase()` в деталях). `POST /workflow/create` требовал вместо
этого ключ `action`, а `POST /workflows/{id}/steps` принимал любой непустой
`type`. Шаг без `type` ронял ВЕСЬ экран деталей, шаг с опечаткой в типе
превращался в безымянную точку. Словарь допустимых типов лежал в движке и не
вызывался ниоткуда. Теперь язык один, проверка одна — `workflow_engine.validate_steps`.

ЧТО БЫЛО С СОСТОЯНИЕМ. `GET /workflow/{id}/status` передавал id СЦЕНАРИЯ в
аргумент, который означает id ПРОГОНА, и отдавал чужой прогон как состояние
этого сценария; если совпадения id не было — тело `null` с кодом 200. А блок
«📊 Последние запуски» на экране деталей был написан по полю `runs`, которого
обработчик не отдавал: разметка существовала и не показывалась никогда.

Чего здесь по-прежнему НЕТ. Сценарий не исполняется: `execute_workflow` только
заводит строку в `workflow_runs` со статусом `running`, шаги не выполняет
никто, запуска по событию и по расписанию нет. Экран об этом говорит прямо
(подсказка на `s-workflows`), и эти обработчики ничего обратного не обещают.

Что маршруты не потерялись, стережёт слепок в
`tests/test_miniapp_routes_are_not_lost.py`.
"""
from __future__ import annotations

import json

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Регистрирует маршруты сценариев на общем приложении мини-аппа."""
    # Общие помощники живут в mini_app_api, а он импортирует этот модуль —
    # импорт сверху дал бы цикл. К моменту вызова mini_app_api уже загружен.
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _safe_fetch,
        _safe_fetchrow, log,
    )
    from services.security import validate_string

    def _wf_id(request: web.Request):
        """id сценария из пути или None."""
        try:
            return int(request.match_info["wf_id"])
        except (KeyError, ValueError, TypeError):
            return None

    def _steps_of(row) -> list:
        """steps из jsonb-колонки: без кодека asyncpg отдаёт строку."""
        steps = row["steps"] if row is not None else None
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except Exception:
                steps = []
        return steps if isinstance(steps, list) else []

    # ── Список и создание ────────────────────────────────────────────────────

    async def workflow_list(request: web.Request) -> web.Response:
        """Список сценариев в форме экрана: статус из is_active, число шагов из
        inline-jsonb, last_run из workflow_runs."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await _safe_fetch(pool,
                """SELECT wd.id, wd.name,
                          CASE WHEN COALESCE(wd.is_active,FALSE) THEN 'active' ELSE 'paused' END AS status,
                          COALESCE(jsonb_array_length(wd.steps), 0) AS step_count,
                          (SELECT MAX(r.started_at) FROM workflow_runs r
                           WHERE r.workflow_id = wd.id) AS last_run
                   FROM workflow_definitions wd
                   WHERE wd.owner_id=$1 ORDER BY wd.name""", uid)
            workflows = [{
                "id": r["id"], "name": r["name"], "status": r["status"],
                "step_count": int(r["step_count"] or 0),
                "last_run": r["last_run"].isoformat() if r["last_run"] else None,
            } for r in (rows or [])]
            return _json_resp({"workflows": workflows})
        except Exception:
            log.exception("workflow_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def workflow_create_plural(request: web.Request) -> web.Response:
        """Создать сценарий (контракт экрана: POST /workflows {name,template?,description?})."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = validate_string(body.get("name"), max_len=120)
        if not name:
            return _err("Укажите название воркфлоу", 400)
        desc = validate_string(body.get("description"), max_len=500) or None
        from services import workflow_engine as _wf
        # Шаблон экран присылал с самого начала («Создать воркфлоу по шаблону
        # "💧 Дрип-серия"?» → «✅ создан»), а обработчик его не читал: сценарий
        # получался пустой. Неизвестный шаблон — отказ, а не пустой сценарий.
        template = (validate_string(body.get("template"), max_len=40,
                                    required=False) or "").strip().lower()
        tpl = _wf.workflow_template(template) if template else None
        if template and not tpl:
            return _err("Неизвестный шаблон сценария", 400)
        try:
            # Шаблоны проверяются тем же словарём, что и шаги от владельца:
            # шаблон, разошедшийся со словарём, нарисовал бы «⚫ undefined» на
            # экране ровно так же.
            steps = _wf.validate_steps(tpl["steps"] if tpl else [])
        except ValueError as e:
            return _err(str(e), 400)
        # Сценарий создаётся ВЫКЛЮЧЕННЫМ: включать то, что ещё не исполняется,
        # значит обещать работу, которой не будет.
        res = await _wf.create_workflow(pool, uid, name,
                                        description=desc or "", steps=steps,
                                        is_active=False)
        if not res.get("ok"):
            log.warning("workflow_create_plural uid=%s: %s", uid, res.get("error"))
            return _err(_INTERNAL_ERROR, 500)
        out = {"ok": True, "id": res["id"], "steps": len(steps)}
        if tpl:
            out.update(template=tpl["key"], template_label=tpl["label"])
        return _json_resp(out)

    async def workflow_create(request: web.Request) -> web.Response:
        """Создать сценарий сразу со шагами (POST /workflow/create {name, steps[]}).

        Язык шагов тот же, что у экрана и у шаблонов: `{"type": ...}`. Раньше
        здесь требовался ключ `action`, которого не присылает ни один экран, а
        шаг без `type` ронял экран деталей целиком.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = validate_string(body.get("name"), max_len=120)
        if not name:
            return _err("Укажите название воркфлоу", 400)
        from services import workflow_engine as _wf
        raw = body.get("steps")
        if not isinstance(raw, list) or not raw:
            return _err("steps обязателен и должен быть непустым списком", 400)
        try:
            steps = _wf.validate_steps(raw)
        except ValueError as e:
            return _err(str(e), 400)
        desc = validate_string(body.get("description"), max_len=500) or ""
        res = await _wf.create_workflow(pool, uid, name, description=desc,
                                        steps=steps, is_active=False)
        if not res.get("ok"):
            log.warning("workflow_create uid=%s: %s", uid, res.get("error"))
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "workflow_id": res["id"],
                           "id": res["id"], "steps": len(steps)})

    # ── Детали и шаги ────────────────────────────────────────────────────────

    async def workflow_detail_plural(request: web.Request) -> web.Response:
        """Детали сценария: {id,name,active,steps[],runs[]}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wid = _wf_id(request)
        if wid is None:
            return _err("Неверный идентификатор", 400)
        row = await _safe_fetchrow(pool,
            "SELECT id, name, description, steps, COALESCE(is_active,FALSE) AS is_active "
            "FROM workflow_definitions WHERE id=$1 AND owner_id=$2", wid, uid)
        if not row:
            return _err("Воркфлоу не найден", 404)
        from services import workflow_engine as _wf
        try:
            runs = await _wf.recent_runs(pool, uid, wid)
        except Exception:
            # Прогоны — дополнение к экрану, а не он сам: без них сценарий всё
            # равно показываем, иначе ошибка в одном блоке гасит весь экран.
            log.exception("workflow_detail runs uid=%s wf=%s", uid, wid)
            runs = []
        return _json_resp({"id": row["id"], "name": row["name"],
                           "description": row["description"] or "",
                           "active": bool(row["is_active"]),
                           "steps": _steps_of(row), "runs": runs})

    async def workflow_add_step(request: web.Request) -> web.Response:
        """Добавить шаг: POST /workflows/{id}/steps {type,...} — аппенд в steps jsonb."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wid = _wf_id(request)
        if wid is None:
            return _err("Неверный идентификатор", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        row = await _safe_fetchrow(pool,
            "SELECT steps FROM workflow_definitions WHERE id=$1 AND owner_id=$2",
            wid, uid)
        if row is None:
            return _err("Воркфлоу не найден", 404)
        from services import workflow_engine as _wf
        try:
            # Уже сохранённые шаги считаются здесь: предел на сценарий имеет
            # смысл только вместе с ними.
            step = _wf.validate_steps([body], already=len(_steps_of(row)))[0]
        except ValueError as e:
            return _err(str(e), 400)
        res = await pool.execute(
            "UPDATE workflow_definitions SET steps = COALESCE(steps,'[]'::jsonb) || $1::jsonb, "
            "updated_at=NOW() WHERE id=$2 AND owner_id=$3",
            json.dumps([step]), wid, uid)
        if isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0":
            return _err("Воркфлоу не найден", 404)
        return _json_resp({"ok": True, "step": step})

    # ── Пауза, активация, удаление ───────────────────────────────────────────

    async def _toggle(request: web.Request, active: bool) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wid = _wf_id(request)
        if wid is None:
            return _err("Неверный идентификатор", 400)
        from services import workflow_engine as _wf
        try:
            res = await (_wf.resume_workflow if active else _wf.pause_workflow)(
                pool, uid, wid)
        except LookupError as e:
            return _err(str(e), 404)
        except Exception:
            log.exception("workflow_toggle uid=%s wf=%s active=%s", uid, wid, active)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, **res})

    async def workflow_toggle_plural(request: web.Request) -> web.Response:
        """Активировать/поставить на паузу: PATCH /workflows/{id} {active}."""
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not isinstance(body, dict):
            return _err("Неверный запрос", 400)
        return await _toggle(request, bool(body.get("active")))

    async def workflow_pause(request: web.Request) -> web.Response:
        return await _toggle(request, False)

    async def workflow_resume(request: web.Request) -> web.Response:
        return await _toggle(request, True)

    async def workflow_delete_any(request: web.Request) -> web.Response:
        """Удалить сценарий. Один путь на оба маршрута — через движок.

        Движок отменяет незавершённые прогоны и отвязывает остальные: без
        этого внешний ключ workflow_runs.workflow_id ронял удаление, а
        «выполняющиеся» прогоны остались бы висеть на несуществующем сценарии.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wid = _wf_id(request)
        if wid is None:
            return _err("Неверный идентификатор", 400)
        from services import workflow_engine as _wf
        try:
            ok = await _wf.delete_workflow(pool, uid, wid)
        except Exception:
            log.exception("workflow_delete uid=%s wf=%s", uid, wid)
            return _err(_INTERNAL_ERROR, 500)
        if not ok:
            # Разницу «нет такого» и «чужой» не раскрываем: иначе по коду
            # ответа можно перебирать чужие id.
            return _err("Воркфлоу не найден", 404)
        return _json_resp({"ok": True})

    # ── Запуск и состояние ───────────────────────────────────────────────────

    async def workflow_execute(request: web.Request) -> web.Response:
        """Завести прогон сценария.

        Шаги этот вызов НЕ выполняет — исполнителя у сценариев нет. Поэтому
        ответ прямо говорит, что прогон только зарегистрирован, а не «пошёл»:
        экран не должен обещать владельцу работу, которой не будет.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wid = _wf_id(request)
        if wid is None:
            return _err("Неверный идентификатор сценария", 400)
        from services import workflow_engine as _wf
        try:
            result = await _wf.execute_workflow(pool, uid, wid)
        except Exception:
            log.exception("workflow_execute uid=%s wf=%s", uid, wid)
            return _err(_INTERNAL_ERROR, 500)
        if not result.get("ok"):
            # Раньше здесь стояло `{"ok": True, **result}`: отказ уходил кодом
            # 200, и экран показывал успех на несуществующем сценарии.
            log.warning("workflow_execute uid=%s wf=%s: %s", uid, wid,
                        result.get("error"))
            return _err("Воркфлоу не найден", 404)
        return _json_resp({"ok": True, "run_id": result.get("run_id"),
                           "total_steps": result.get("total_steps", 0),
                           "executed": False,
                           "note": "Прогон зарегистрирован. Шаги пока не "
                                   "выполняются: исполнителя сценариев нет."})

    async def workflow_status(request: web.Request) -> web.Response:
        """Состояние сценария — его ПОСЛЕДНИЙ прогон (GET /workflow/{id}/status)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wid = _wf_id(request)
        if wid is None:
            return _err("Неверный идентификатор сценария", 400)
        from services import workflow_engine as _wf
        owns = await _safe_fetchrow(pool,
            "SELECT 1 FROM workflow_definitions WHERE id=$1 AND owner_id=$2",
            wid, uid)
        if not owns:
            return _err("Воркфлоу не найден", 404)
        try:
            run = await _wf.latest_run(pool, uid, wid)
        except Exception:
            log.exception("workflow_status uid=%s wf=%s", uid, wid)
            return _err(_INTERNAL_ERROR, 500)
        if not run:
            # Пустое тело `null` с кодом 200 экран читал как «что-то есть».
            return _json_resp({"ok": True, "run": None,
                               "note": "Сценарий ещё не запускали."})
        return _json_resp({"ok": True, "run": run})

    app.router.add_get("/api/miniapp/workflows", workflow_list)
    app.router.add_post("/api/miniapp/workflows", workflow_create_plural)
    app.router.add_get("/api/miniapp/workflows/{wf_id}", workflow_detail_plural)
    app.router.add_patch("/api/miniapp/workflows/{wf_id}", workflow_toggle_plural)
    app.router.add_delete("/api/miniapp/workflows/{wf_id}", workflow_delete_any)
    app.router.add_post("/api/miniapp/workflows/{wf_id}/steps", workflow_add_step)
    app.router.add_post("/api/miniapp/workflow/create", workflow_create)
    app.router.add_post("/api/miniapp/workflow/{wf_id}/execute", workflow_execute)
    app.router.add_get("/api/miniapp/workflow/{wf_id}/status", workflow_status)
    app.router.add_post("/api/miniapp/workflow/{wf_id}/pause", workflow_pause)
    app.router.add_post("/api/miniapp/workflow/{wf_id}/resume", workflow_resume)
    app.router.add_delete("/api/miniapp/workflow/{wf_id}", workflow_delete_any)
