"""Рабочие пространства, папки чатов и память ассистента — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 13 маршрутов трёх блоков про
обустройство рабочего места владельца.

* `/api/miniapp/workspace*` — пространства: список, создание, карточка с
  участниками, приглашение, вход по приглашению, исключение участника и
  выход. Приглашение живёт ограниченное время
  (`tests/test_workspace_invite_expires.py`), а участники обязаны быть видны
  в приложении (`tests/test_workspace_members_visible_in_app.py`).
* `/api/miniapp/chatlist_folders*` — папки чатов: список, создание, удаление.
* `/api/miniapp/ai_memory*` — заметки ассистента: список, добавление,
  удаление.

Вместе с обработчиками уехал словарь `_WS_REMOVE_ERR` — РУССКИЕ причины
отказа при исключении участника вместе с кодом ответа, по одной на каждый
исход движка. Он на уровне `setup_routes`, а не внутри обработчика, потому
что причина отказа — часть договора экрана с ручкой, и держать её рядом с
единственным её читателем честнее, чем прятать в тело.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты пространств, папок чатов и памяти ассистента."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _safe_count,
        _safe_fetch, log,
    )
    from services.security import validate_string

    _WS_REMOVE_ERR = {
        "forbidden": ("Исключать участников может только владелец или "
                      "администратор пространства", 403),
        "not_found": ("Участник не найден", 404),
        "owner": ("Владельца пространства исключить нельзя — пространство "
                  "осталось бы без хозяина", 400),
        "self": ("Себя исключить нельзя: выйдите из пространства", 400),
    }

    async def ai_memory_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, kind, title, body, tags, pinned, created_at, updated_at
                   FROM botmother_memory WHERE owner_id=$1
                   ORDER BY pinned DESC, updated_at DESC LIMIT $2""",
                uid, _list_limit(request, 50, 1000),
            )
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM botmother_memory WHERE owner_id=$1", uid)
            return _json_resp({"total": total, "memories": [
                {
                    **dict(r),
                    "tags": list(r["tags"] or []),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]})
        except Exception:
            log.exception("ai_memory_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ai_memory_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        title = (body.get("title") or "").strip()
        mem_body = (body.get("body") or "").strip()
        if not mem_body:
            return _err("Заполните содержимое", 400)
        try:
            row = await pool.fetchrow(
                """INSERT INTO botmother_memory(owner_id, kind, title, body, source)
                   VALUES($1,'note',$2,$3,'miniapp') RETURNING id""",
                uid, title, mem_body,
            )
            return _json_resp({"id": row["id"]})
        except Exception:
            log.exception("ai_memory_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ai_memory_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mem_id = int(request.match_info["mem_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор участника", 400)
        try:
            await pool.execute(
                "DELETE FROM botmother_memory WHERE id=$1 AND owner_id=$2", mem_id, uid
            )
            return _json_resp({"ok": True})
        except Exception:
            log.exception("ai_memory_delete uid=%d mem=%d", uid, mem_id)
            return _err(_INTERNAL_ERROR, 500)

    async def chatlist_folders_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, title, chat_count, status, invite_link, error,
                      instance_id, join_count, created_at
                 FROM chatlist_folders WHERE owner_id=$1
                ORDER BY created_at DESC LIMIT $2""", uid, _list_limit(request, 100, 2000))
        out = []
        for r in (rows or []):
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            out.append(d)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM chatlist_folders WHERE owner_id=$1", uid)
        return _json_resp({"ok": True, "folders": out, "total": total})

    async def chatlist_folder_create(request: web.Request) -> web.Response:
        """Собрать папку из своих каналов/чатов и поставить экспорт ссылки в очередь.

        Пригодность набора проверяем СРАЗУ (пусто/слишком много/непригодные) —
        отказ до операции; сам экспорт ссылки идёт операцией (сессия аккаунта,
        баноопасность, Premium-гейт Telegram)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        from services import chatlist_folders as cf

        # Разрешаем два источника набора: явные chat_ids ИЛИ узлы связки.
        chat_ids = list(body.get("chat_ids") or [])
        instance_id = body.get("instance_id")
        # Своя карта каналов: id строки И telegram channel_id → channel_id.
        # Соглашение ref_id в узлах связки неоднозначно (id строки vs channel_id),
        # поэтому резолвим против ОБОИХ и берём настоящий channel_id.
        owned_rows = await _safe_fetch(pool,
            "SELECT id, channel_id, title FROM managed_channels WHERE owner_id=$1", uid)
        by_row_id, by_chan_id, owned_ids = {}, {}, set()
        for r in (owned_rows or []):
            cid = int(r["channel_id"])
            owned_ids.add(cid)
            by_chan_id[cid] = cid
            by_row_id[int(r["id"])] = cid

        # Сборка из связки: если явных чатов нет, а есть instance_id — тянем узлы
        # (owner-scoped) и резолвим их ref_id в свои каналы. Так «развернул
        # связку → раздал папку» становится одной цепочкой, а не двумя экранами.
        auto_title = None
        if not chat_ids and instance_id:
            from services import network_builder as _nb

            detail = await _nb.get_instance_detail(pool, uid, int(instance_id))
            if not detail:
                return _err("Связка не найдена", 404)
            auto_title = (detail.get("instance") or {}).get("name")
            for n in detail.get("nodes") or []:
                if str(n.get("node_type") or "").lower() not in ("channel", "group", "supergroup", "chat"):
                    continue
                ref = n.get("ref_id")
                try:
                    ref = int(ref)
                except (TypeError, ValueError):
                    continue
                cid = by_chan_id.get(ref) or by_row_id.get(ref)
                if cid:
                    chat_ids.append(cid)
            if not chat_ids:
                return _err("В связке нет ваших каналов, пригодных для папки.", 400)

        # Отбор своих чатов, запись папки и постановка экспорта — одна дорога
        # со связками модулей (op_chain «создал каналы → собрал папку»).
        try:
            r = await cf.submit_folder(pool, uid, chat_ids,
                                       body.get("title") or auto_title or "Подборка",
                                       acc_id=body.get("acc_id"), instance_id=instance_id)
        except PermissionError as exc:
            # Отказ по тарифу — не сбой: честный 403 с причиной, чтобы платная
            # функция не выглядела сломанной.
            return _err(str(exc) or "Требуется подписка", 403)
        if not r.get("ok"):
            return _err(r.get("reason") or "Папку собрать не удалось", r.get("status") or 400)
        return _json_resp({"ok": True, "folder_id": r["folder_id"], "op_id": r["op_id"],
                           "chat_count": r["chat_count"]})

    async def chatlist_folder_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["folder_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор папки", 400)
        row = await pool.fetchrow(
            "DELETE FROM chatlist_folders WHERE id=$1 AND owner_id=$2 RETURNING id",
            fid, uid)
        if not row:
            return _err("Папка не найдена", 404)
        return _json_resp({"ok": True})

    async def workspaces_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT w.id, w.name, w.description,
                          (SELECT COUNT(*) FROM workspace_members WHERE workspace_id=w.id) AS member_count,
                          wm.role
                   FROM workspaces w
                   JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=$1
                   WHERE w.is_active=TRUE ORDER BY w.created_at DESC""",
                uid,
            )
        except Exception:
            log.exception("workspaces_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"workspaces": [
            {
                "id": r["id"], "name": r["name"] or "",
                "description": r["description"] or "",
                "member_count": int(r["member_count"] or 0),
                "role": r["role"] or "member",
            }
            for r in rows
        ]})

    async def create_workspace(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            name = str(body.get("name", "")).strip()[:100]
            description = str(body.get("description", "")).strip()[:500]
        except Exception:
            return _err("Неверное тело запроса", 400)
        if not name:
            return _err("name обязателен", 400)
        try:
            ws_id = await pool.fetchval(
                "INSERT INTO workspaces(owner_id, name, description) VALUES($1,$2,$3) RETURNING id",
                uid, name, description or None,
            )
            await pool.execute(
                "INSERT INTO workspace_members(workspace_id, user_id, role, invited_by) VALUES($1,$2,'owner',$2)",
                ws_id, uid,
            )
        except Exception:
            log.exception("create_workspace uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "ws_id": ws_id})

    async def leave_workspace(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ws_id = int(request.match_info["ws_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор рабочего пространства", 400)
        # Check if owner — owners must delete instead of leave
        try:
            role = await pool.fetchval(
                "SELECT role FROM workspace_members WHERE workspace_id=$1 AND user_id=$2",
                ws_id, uid,
            )
        except Exception:
            log.exception("leave_workspace role uid=%d ws=%d", uid, ws_id)
            return _err(_INTERNAL_ERROR, 500)
        if not role:
            return _err("Вы не участник", 404)
        try:
            from database.db import record_manual_action

            if role == "owner":
                # Delete workspace entirely
                await pool.execute("DELETE FROM workspaces WHERE id=$1 AND owner_id=$2", ws_id, uid)
                # Удаление пространства снимает доступ СО ВСЕХ его участников —
                # событие того же веса, что и вход в чужое пространство,
                # который уже пишется (workspace_member_join).
                await record_manual_action(
                    pool, uid, "workspace_delete", target=f"ws:{ws_id}")
            else:
                await pool.execute(
                    "DELETE FROM workspace_members WHERE workspace_id=$1 AND user_id=$2", ws_id, uid
                )
                await record_manual_action(
                    pool, uid, "workspace_member_leave", target=f"ws:{ws_id} user:{uid}")
            return _json_resp({"ok": True})
        except Exception:
            log.exception("leave_workspace delete uid=%d ws=%d", uid, ws_id)
            return _err(_INTERNAL_ERROR, 500)

    async def workspace_detail(request: web.Request) -> web.Response:
        """Состав пространства. Доступ — только участникам.

        Экран списка показывал «3 участника» и кнопку выхода: кто эти трое,
        увидеть было нельзя ни здесь, ни где-либо ещё в мини-аппе, хотя вход в
        пространство даёт доступ к ботам владельца (токен отдаётся
        расшифрованным) и к его каналам. Состав и выписка приглашений в
        продукте были, но только в Telegram-боте.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ws_id = int(request.match_info["ws_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор рабочего пространства", 400)
        try:
            from database.db import (
                WORKSPACE_INVITE_ROLES, WORKSPACE_INVITE_TTL_DAYS,
                get_workspace, get_workspace_members, get_workspace_role,
            )
            role = await get_workspace_role(pool, ws_id, uid)
            if not role:
                return _err("Пространство не найдено", 404)
            ws = await get_workspace(pool, ws_id, viewer_id=uid)
            if not ws:
                return _err("Пространство не найдено", 404)
            members = await get_workspace_members(pool, ws_id, viewer_id=uid)
            live = await _safe_count(pool,
                "SELECT COUNT(*) FROM workspace_invites WHERE workspace_id=$1 "
                "AND uses_left>0 AND now() < COALESCE(expires_at, created_at "
                f"+ INTERVAL '{WORKSPACE_INVITE_TTL_DAYS} days')", ws_id)
            return _json_resp({
                "workspace": {
                    "id": ws_id,
                    "name": ws.get("name") or "",
                    "description": ws.get("description") or "",
                    "created_at": ws["created_at"].isoformat() if ws.get("created_at") else None,
                },
                "my_role": role,
                "can_invite": role in WORKSPACE_INVITE_ROLES,
                "invite_ttl_days": WORKSPACE_INVITE_TTL_DAYS,
                "live_invites": live,
                "members": [
                    {
                        "user_id": m["user_id"],
                        "role": m["role"] or "member",
                        "username": m["username"] or "",
                        "first_name": m["first_name"] or "",
                        "joined_at": m["joined_at"].isoformat() if m["joined_at"] else None,
                        "is_me": int(m["user_id"]) == int(uid),
                    }
                    for m in members
                ],
            })
        except Exception:
            log.exception("workspace_detail uid=%d ws=%d", uid, ws_id)
            return _err(_INTERNAL_ERROR, 500)

    async def workspace_invite_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ws_id = int(request.match_info["ws_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор рабочего пространства", 400)
        try:
            from database.db import WORKSPACE_INVITE_TTL_DAYS, create_workspace_invite

            # Право приглашать проверяет сама create_workspace_invite — она
            # единственный источник правды и для бота тоже.
            code = await create_workspace_invite(pool, ws_id, uid)
            if code is None:
                return _err("Приглашать может только владелец или администратор "
                            "пространства", 403)
            return _json_resp({"ok": True, "code": code,
                               "ttl_days": WORKSPACE_INVITE_TTL_DAYS, "uses": 5})
        except Exception:
            log.exception("workspace_invite_create uid=%d ws=%d", uid, ws_id)
            return _err(_INTERNAL_ERROR, 500)

    async def workspace_join(request: web.Request) -> web.Response:
        """Войти по коду приглашения — то же, что «Войти по коду» в боте."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        code = validate_string(body.get("code"), 100, required=True)
        if not code:
            return _err("Введите код приглашения", 400)
        try:
            from database.db import use_workspace_invite

            ws_id = await use_workspace_invite(pool, code, uid)
            if not ws_id:
                return _err("Код неверный или истёк", 404)
            return _json_resp({"ok": True, "ws_id": ws_id})
        except Exception:
            log.exception("workspace_join uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def workspace_member_remove(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ws_id = int(request.match_info["ws_id"])
            member_id = int(request.match_info["member_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        try:
            from database.db import remove_workspace_member

            res = await remove_workspace_member(pool, ws_id, uid, member_id)
            if res != "ok":
                msg, code = _WS_REMOVE_ERR.get(res, ("Не удалось исключить", 400))
                return _err(msg, code)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("workspace_member_remove uid=%d ws=%d member=%d",
                          uid, ws_id, member_id)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/workspaces", workspaces_list)
    app.router.add_post("/api/miniapp/workspace", create_workspace)
    app.router.add_delete("/api/miniapp/workspace/{ws_id}", leave_workspace)
    app.router.add_get("/api/miniapp/workspace/{ws_id}", workspace_detail)
    app.router.add_post("/api/miniapp/workspace/{ws_id}/invite", workspace_invite_create)
    app.router.add_post("/api/miniapp/workspace/join", workspace_join)
    app.router.add_delete("/api/miniapp/workspace/{ws_id}/member/{member_id}", workspace_member_remove)
    app.router.add_get("/api/miniapp/chatlist_folders", chatlist_folders_list)
    app.router.add_post("/api/miniapp/chatlist_folders", chatlist_folder_create)
    app.router.add_delete("/api/miniapp/chatlist_folders/{folder_id}", chatlist_folder_delete)
    app.router.add_get("/api/miniapp/ai_memory", ai_memory_list)
    app.router.add_post("/api/miniapp/ai_memory", ai_memory_create)
    app.router.add_delete("/api/miniapp/ai_memory/{mem_id}", ai_memory_delete)
